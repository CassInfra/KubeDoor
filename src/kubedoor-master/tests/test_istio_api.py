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
from auth import audit, permissions  # noqa: E402
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


# 旧版 agent 没有的接口:本地服务回 404 纯文本,agent 解析 JSON 失败
OLD_AGENT_ERROR = {'success': False, 'error': "404, message='Attempt to decode JSON with unexpected mimetype: "
                                              "text/plain; charset=utf-8', url='https://localhost/'"}


class FakeAgent:
    """按集群保存 VirtualService;列出 / 查看 / 下发 / 删除接口和真 agent 的返回格式一致。"""

    def __init__(self):
        self.clusters = {}
        self.offline = set()
        self.old_agent = set()  # 还没有查看单个 / 删除接口的旧版 agent
        self.lost = set()  # 改集群的请求等不到回应(超时,没生效)
        self.applied = []
        self.deleted = []

    def put(self, k8s_name, name, spec):
        self.clusters.setdefault(k8s_name, {})[(NS, name)] = copy.deepcopy(spec)

    async def call(self, env, path, query=None, method='GET', body=None, timeout=30):
        if env in self.offline:
            raise Exception(f'目标客户端 {env} 不在线')
        if env in self.lost and method != 'GET':
            raise Exception(f'等待 {env} 对 {path} 的响应超时')
        items = self.clusters.setdefault(env, {})
        if path == handlers.AGENT_LIST_PATH and method == 'GET':
            data = [{'name': name, 'namespace': ns, 'resourceVersion': '1', 'spec': copy.deepcopy(spec)}
                    for (ns, name), spec in items.items()]
            return {'success': True, 'data': data, 'total': len(data)}
        if path == handlers.AGENT_DETAIL_PATH and method == 'GET':
            if env in self.old_agent:
                return OLD_AGENT_ERROR
            key = (query['namespace'], query['name'])
            if key not in items:
                return {'success': False, 'status': 404, 'error': f'VirtualService {key[0]}/{key[1]} not found'}
            metadata = {'name': key[1], 'namespace': key[0], 'resourceVersion': '1', 'generation': 3,
                        'managedFields': [{'manager': 'kubedoor-agent', 'operation': 'Apply'}],
                        'annotations': {S.LAST_APPLIED_ANNOTATION: json.dumps({'spec': items[key]}), 'note': 'kept'}}
            return {'success': True, 'data': {'apiVersion': 'networking.istio.io/v1', 'kind': 'VirtualService',
                                              'metadata': metadata, 'spec': copy.deepcopy(items[key])}}
        if path == handlers.AGENT_APPLY_PATH and method == 'POST':
            self.applied.append((env, copy.deepcopy(body)))
            if body['spec'].get('hosts') == ['reject.example.com']:
                return {'success': False, 'status': 422, 'error': 'Kubernetes API错误: Unprocessable Entity'}
            meta = body['metadata']
            items[(meta['namespace'], meta['name'])] = copy.deepcopy(body['spec'])
            return {'success': True, 'message': f"VirtualService {meta['name']} applied successfully"}
        if path == handlers.AGENT_DELETE_PATH and method == 'DELETE':
            if env in self.old_agent:
                return OLD_AGENT_ERROR
            key = (query['namespace'], query['name'])
            if key not in items:
                return {'success': False, 'status': 404, 'error': f'VirtualService {key[1]} not found'}
            self.deleted.append((env, key))
            del items[key]
            return {'success': True, 'message': f'VirtualService {key[1]} deleted successfully'}
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
    await db.pg_execute('TRUNCATE istio_group, istio_vs_backup RESTART IDENTITY CASCADE')
    # 权限与审批用库里的(默认值),不用 conftest 放进缓存的那份
    await db.pg_execute("DELETE FROM auth_settings WHERE key = 'permission_policy'")
    permissions.invalidate_cache()
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
    assert 'match.uri.prefix: /b2/' in rows['b']['search_lines']  # 页面全文搜索用的规则内容

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
    # 搜索内容也只来自能看的集群:/b2/ 只在 beta-k 里
    only_p = {'X-Test-Role': 'none', 'X-Test-Clusters': json.dumps({'beta-p': 'read'})}
    assert 'match.uri.prefix: /b2/' not in (await matrix(client, group_id, headers=only_p))['b']['search_lines']
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

    # 一个集群可以属于多个组(多个项目共用的测试集群)
    other = await call(client, 'POST', '/api/istio/groups', {
        'name': 'other', 'clusters': [{'k8s_name': 'beta-k', 'env_type': 'test'}, {'k8s_name': 'prod-b', 'env_type': 'prod'}],
        'include_patterns': [{'type': 'glob', 'pattern': 'b*'}]})
    other_id = other['data']['id']
    dup = await call(client, 'POST', '/api/istio/groups', {'name': 'cassmall'}, status=400)
    assert '已存在' in dup['error']
    bad_type = await call(client, 'POST', '/api/istio/groups', {
        'name': 'x', 'clusters': [{'k8s_name': 'prod-b', 'env_type': 'dev'}]}, status=400)
    assert '集群类型' in bad_type['error']
    bad_regex = await call(client, 'POST', '/api/istio/groups', {
        'name': 'x', 'ignore_patterns': [{'type': 'glob', 'pattern': 'ok-*'}, {'type': 'regex', 'pattern': 'a(b'}]},
        status=400)
    assert '忽略规则第 2 条「a(b」正则写法有误' in bad_regex['error']

    groups = (await call(client, 'GET', '/api/istio/groups'))['data']
    owners = {c['k8s_name']: c['groups'] for c in groups['clusters']}
    assert owners['beta-k'] == ['cassmall', 'other'] and owners['prod-b'] == ['other'] and owners['prod-a'] == ['cassmall']
    saved = next(g for g in groups['groups'] if g['id'] == other_id)
    assert saved['include_patterns'] == [{'type': 'glob', 'pattern': 'b*'}] and saved['ignore_patterns'] == []
    beta_k = next(c for c in saved['clusters'] if c['k8s_name'] == 'beta-k')
    assert beta_k['collected_at'] and beta_k['vs_count'] == 2  # 采集状态按集群,各组共用

    # 集群离线:只有它失败;刷新结果给出本组的个数
    agent.offline.add('beta-p')
    collected = {r['k8s_name']: r for r in (await call(client, 'POST', '/api/istio/collect',
                                                         {'group_id': group_id}))['data']}
    assert collected['beta-k']['success'] and not collected['beta-p']['success']
    assert '不在线' in collected['beta-p']['error']
    other_collected = (await call(client, 'POST', '/api/istio/collect', {'group_id': other_id,
                                                                          'k8s_names': ['beta-k']}))['data'][0]
    assert (other_collected['count'], other_collected['group_count'], other_collected['ignored_count']) == (2, 1, 1)
    outside = await call(client, 'POST', '/api/istio/collect', {'group_id': group_id, 'k8s_names': ['prod-b']},
                         status=400)
    assert '不在组' in outside['error']

    # 把 beta-k、prod-a 移出 cassmall:本组在它们上的部署关系删掉;beta-k 还在 other 组,现状保留,prod-a 的删掉
    await call(client, 'PUT', f'/api/istio/groups?id={group_id}', {
        'name': 'cassmall', 'clusters': [{'k8s_name': 'beta-p', 'env_type': 'test'},
                                         {'k8s_name': 'prod-b', 'env_type': 'prod'}]})
    rows = await matrix(client, group_id)
    assert list(rows['a']['cells']) == ['beta-p', 'prod-b']
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_cluster WHERE k8s_name IN ('beta-k', 'prod-a')") == 0
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_snapshot WHERE k8s_name = 'beta-k'") == 2
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_snapshot WHERE k8s_name = 'prod-a'") == 0
    assert await db.pg_fetchval("SELECT count(*) FROM istio_cluster_state WHERE k8s_name = 'prod-a'") == 0

    await call(client, 'DELETE', '/api/istio/groups', id=group_id)
    assert await db.pg_fetchval('SELECT count(*) FROM istio_vs') == 0
    await call(client, 'DELETE', '/api/istio/groups', id=group_id, status=404)
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_snapshot WHERE k8s_name = 'beta-p'") == 0
    assert await db.pg_fetchval("SELECT count(*) FROM istio_vs_snapshot WHERE k8s_name = 'beta-k'") == 2


async def two_groups(client, agent):
    """共用测试集群 beta-k / beta-p 的两个项目组:
    A = beta + prod-a,忽略 b-*;B = beta + prod-b,只包含 b-* 和正则 x-.*。x-shared 两个组都包含(冲突)。"""
    for cluster in ('beta-k', 'beta-p'):
        for name in ('a-one', 'a-two', 'b-one', 'x-shared'):
            agent.put(cluster, name, vs([f'{name}.beta.x.com'], [route(f'/{name}/', 1)]))
    agent.put('prod-a', 'a-one', vs(['a-one.x.com'], [route('/a-one/', 1)]))
    agent.put('prod-b', 'b-one', vs(['b-one.x.com'], [route('/b-one/', 1)]))
    a = await call(client, 'POST', '/api/istio/groups', {
        'name': 'A', 'clusters': [{'k8s_name': 'beta-k'}, {'k8s_name': 'beta-p'}, {'k8s_name': 'prod-a', 'env_type': 'prod'}],
        'ignore_patterns': [{'type': 'glob', 'pattern': 'b-*'}]})
    b = await call(client, 'POST', '/api/istio/groups', {
        'name': 'B', 'clusters': [{'k8s_name': 'beta-k'}, {'k8s_name': 'beta-p'}, {'k8s_name': 'prod-b', 'env_type': 'prod'}],
        'include_patterns': [{'type': 'glob', 'pattern': 'b-*'}, {'type': 'regex', 'pattern': 'x-.*'}]})
    a_id, b_id = a['data']['id'], b['data']['id']
    await call(client, 'POST', '/api/istio/collect', {'group_id': a_id})
    await call(client, 'POST', '/api/istio/collect', {'group_id': b_id, 'k8s_names': ['prod-b']})
    return a_id, b_id


async def matrix_data(client, group_id):
    return (await call(client, 'GET', '/api/istio/matrix', group_id=group_id))['data']


@pytest.mark.asyncio
async def test_group_scope_ignore_and_conflicts(env):
    client, agent, _ = env
    a_id, b_id = await two_groups(client, agent)

    data = await matrix_data(client, a_id)
    rows = {r['name']: r for r in data['rows']}
    assert sorted(rows) == ['a-one', 'a-two', 'x-shared']
    assert [(c['group'], c['clusters']) for c in rows['x-shared']['conflicts']] == [('B', ['beta-k', 'beta-p'])]
    assert rows['a-one']['conflicts'] == []
    assert [(i['name'], i['reason'], i['pattern'], i['clusters']) for i in data['ignored']] == [
        ('b-one', S.SCOPE_IGNORE_RULE, 'b-*', ['beta-k', 'beta-p'])]
    assert data['stats'] == {'total': 4, 'visible': 3, 'ignored': 1, 'manual': 0, 'by_rule': 1, 'conflict': 1}
    b_data = await matrix_data(client, b_id)
    assert sorted(r['name'] for r in b_data['rows']) == ['b-one', 'x-shared']
    assert {i['name']: i['reason'] for i in b_data['ignored']} == {'a-one': S.SCOPE_NOT_INCLUDED,
                                                                   'a-two': S.SCOPE_NOT_INCLUDED}

    # 冲突的不能纳管;在 B 里忽略后 A 才能纳管
    blocked = (await call(client, 'POST', '/api/istio/import', {'group_id': a_id, 'items': [
        {'namespace': NS, 'name': 'x-shared', 'base': 'beta-k', 'clusters': ['beta-k']}]}))['data'][0]
    assert not blocked['success'] and '也在组 B(共用集群 beta-k、beta-p)里' in blocked['error']
    ignored = await call(client, 'POST', '/api/istio/ignore', {'group_id': b_id, 'items': [{'namespace': NS, 'name': 'x-shared'}]})
    assert '已忽略 1 个' in ignored['msg']
    b_data = await matrix_data(client, b_id)
    assert [r['name'] for r in b_data['rows']] == ['b-one']
    manual = next(i for i in b_data['ignored'] if i['name'] == 'x-shared')
    assert manual['reason'] == S.SCOPE_MANUAL and manual['by'] == 'admin' and manual['at']
    assert b_data['stats']['manual'] == 1 and b_data['stats']['by_rule'] == 2
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': a_id, 'items': [
        {'namespace': NS, 'name': n, 'base': 'beta-k', 'clusters': ['beta-k', 'beta-p']} for n in ('x-shared', 'a-one')]})
    assert all(r['success'] for r in imported['data'])
    assert all(not r['conflicts'] for r in (await matrix_data(client, a_id))['rows'])

    # 被组规则排除的取消忽略 → 单独纳入本组(于是和 B 冲突);再忽略回去
    await call(client, 'POST', '/api/istio/unignore', {'group_id': a_id, 'items': [{'namespace': NS, 'name': 'b-one'}]})
    rows = {r['name']: r for r in (await matrix_data(client, a_id))['rows']}
    assert rows['b-one']['included'] and rows['b-one']['conflicts'][0]['group'] == 'B'
    await call(client, 'POST', '/api/istio/ignore', {'group_id': a_id, 'items': [{'namespace': NS, 'name': 'b-one'}]})
    assert 'b-one' not in {r['name'] for r in (await matrix_data(client, a_id))['rows']}

    # B 的规则改成也包含 a-one:冲突,A 里已纳管的 a-one 不能下发;预览给出原因
    a_one = rows['a-one']['vs_id']
    await call(client, 'PUT', f'/api/istio/groups?id={b_id}', {
        'name': 'B', 'clusters': [{'k8s_name': 'beta-k'}, {'k8s_name': 'beta-p'}, {'k8s_name': 'prod-b', 'env_type': 'prod'}],
        'include_patterns': [{'type': 'glob', 'pattern': 'b-*'}, {'type': 'regex', 'pattern': 'x-.*'},
                             {'type': 'glob', 'pattern': 'istio-system/a-on?'}]})
    preview = (await call(client, 'POST', '/api/istio/apply/preview', {'items': [{'vs_id': a_one, 'k8s_name': 'beta-k'}]}))['data'][0]
    assert '也在组 B' in preview['error'] and preview['diff'] is None
    denied = await call(client, 'POST', '/api/istio/apply', {'items': [{'vs_id': a_one, 'k8s_name': 'beta-k'}]}, status=400)
    assert '同一个 VS 只能属于一个组' in denied['error']
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=a_one))['data']
    assert detail['scope']['visible'] and detail['conflicts'][0]['group'] == 'B'

    # A 里忽略已纳管的 a-one:组标准保留但不显示、不能下发;取消忽略后恢复
    await call(client, 'POST', '/api/istio/ignore', {'group_id': a_id, 'items': [{'namespace': NS, 'name': 'a-one'}]})
    data = await matrix_data(client, a_id)
    assert 'a-one' not in {r['name'] for r in data['rows']}
    assert next(i for i in data['ignored'] if i['name'] == 'a-one')['vs_id'] == a_one
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=a_one))['data']
    assert not detail['scope']['visible'] and detail['scope']['reason'] == S.SCOPE_MANUAL
    denied = await call(client, 'POST', '/api/istio/apply', {'items': [{'vs_id': a_one, 'k8s_name': 'beta-k'}]}, status=400)
    assert '手动忽略' in denied['error']
    await call(client, 'POST', '/api/istio/unignore', {'group_id': a_id, 'items': [{'namespace': NS, 'name': 'a-one'}]})
    assert 'a-one' in {r['name'] for r in (await matrix_data(client, a_id))['rows']}
    assert await db.pg_fetchval("SELECT count(*) FROM istio_group_vs_override WHERE name = 'a-one'") == 0

    # 手动忽略过、集群里已删掉的,仍列在忽略里(可以取消),不计入总数
    await call(client, 'POST', '/api/istio/ignore', {'group_id': a_id, 'items': [{'namespace': NS, 'name': 'gone'}]})
    data = await matrix_data(client, a_id)
    gone = next(i for i in data['ignored'] if i['name'] == 'gone')
    assert gone['stale'] and gone['clusters'] == [] and data['stats']['total'] == 4

    # 组管理里预览规则:按所选集群的现状,不保存
    preview = (await call(client, 'GET', '/api/istio/groups/preview', group_id=b_id, clusters='beta-k,prod-b,new-c',
                          include=json.dumps([{'type': 'glob', 'pattern': '*-one'}]), ignore='[]'))['data']
    assert (preview['total'], preview['visible'], preview['ignored']) == (4, 2, 2)
    assert preview['uncollected'] == ['new-c']
    items = {i['name']: i for i in preview['items']}
    assert items['b-one']['clusters'] == ['beta-k', 'prod-b'] and items['a-one']['conflicts'][0]['group'] == 'A'
    assert items['x-shared']['reason'] == S.SCOPE_MANUAL  # B 里手动忽略的,预览也算上
    bad = await call(client, 'GET', '/api/istio/groups/preview', clusters='beta-k', include='{bad', status=400)
    assert 'JSON' in bad['error']


@pytest.mark.asyncio
async def test_create_vs_checks_group_scope(env):
    client, agent, _ = env
    a_id, b_id = await two_groups(client, agent)
    body = {'group_id': a_id, 'namespace': NS, 'gateways': ['istio-system/internal'],
            'clusters': [{'k8s_name': 'beta-k', 'hosts': ['n.x.com']}]}

    # 名称匹配本组的忽略规则:先提示,确认后单独纳入
    check = (await call(client, 'GET', '/api/istio/vs/check', group_id=a_id, namespace=NS, name='b-new'))['data']
    assert check['scope'] == {'visible': False, 'reason': S.SCOPE_IGNORE_RULE, 'pattern': 'b-*'}
    assert not check['standard'] and not check['in_clusters'] and not check['conflicts']
    excluded = await call(client, 'POST', '/api/istio/vs', {**body, 'name': 'b-new'}, status=400)
    assert '匹配本组的忽略规则「b-*」' in excluded['error'] and '单独纳入本组' in excluded['error']
    created = await call(client, 'POST', '/api/istio/vs', {**body, 'name': 'b-new', 'force_include': True})
    rows = {r['name']: r for r in (await matrix_data(client, a_id))['rows']}
    assert rows['b-new']['vs_id'] == created['data']['id'] and rows['b-new']['included']

    # 其他组已有(B 包含 b-* 的话 b-new 会冲突):B 再建同名的被拒绝
    taken = await call(client, 'POST', '/api/istio/vs', {**body, 'group_id': b_id, 'name': 'b-new'}, status=400)
    assert '组 A(共用集群 beta-k、beta-p)里已经有' in taken['error']
    # 集群里已有的要用纳管;本组被忽略的提示先取消忽略
    exists = await call(client, 'POST', '/api/istio/vs', {**body, 'group_id': b_id, 'name': 'a-two'}, status=400)
    assert '纳管' in exists['error'] and '取消忽略' in exists['error']
    check = (await call(client, 'GET', '/api/istio/vs/check', group_id=b_id, namespace=NS, name='a-two'))['data']
    assert check['in_clusters'] == ['beta-k', 'beta-p'] and check['scope']['reason'] == S.SCOPE_NOT_INCLUDED


@pytest.mark.asyncio
async def test_delete_vs_in_cluster(env):
    client, agent, audits = env
    a_id, _ = await two_groups(client, agent)
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': a_id, 'items': [
        {'namespace': NS, 'name': 'a-one', 'base': 'beta-k', 'clusters': ['beta-k', 'prod-a']}]})
    vs_id = imported['data'][0]['vs_id']

    # 删除生产集群里的 a-one:集群里没了,本组不再部署到该集群,审计里留着删除前的内容
    deleted = await call(client, 'POST', '/api/istio/live/delete',
                         {'group_id': a_id, 'k8s_name': 'prod-a', 'namespace': NS, 'name': 'a-one'})
    assert deleted['msg'] == '已从集群 prod-a 删除 istio-system/a-one,并且本组不再部署到该集群'
    assert agent.deleted == [('prod-a', (NS, 'a-one'))]
    assert (NS, 'a-one') not in agent.clusters['prod-a']
    record = audits[-1]
    assert record[1] == 'istio.delete' and record[2]['env'] == 'prod-a' and record[2]['success']
    assert record[2]['detail']['spec']['hosts'] == ['a-one.x.com'] and record[2]['detail']['unbound']
    cells_now = (await matrix(client, a_id))['a-one']['cells']
    assert cells_now['prod-a']['status'] == S.NONE and not cells_now['prod-a']['bound']
    assert cells_now['beta-k']['bound']

    # 再删一次:集群里已经没有了
    again = await call(client, 'POST', '/api/istio/live/delete',
                       {'group_id': a_id, 'k8s_name': 'prod-a', 'namespace': NS, 'name': 'a-one'})
    assert again['msg'] == '集群 prod-a 里已经没有 istio-system/a-one'

    # 旧版 agent 没有删除接口:提示升级,集群和部署关系都不动
    agent.old_agent.add('beta-k')
    old = await call(client, 'POST', '/api/istio/live/delete',
                     {'group_id': a_id, 'k8s_name': 'beta-k', 'namespace': NS, 'name': 'a-one'}, status=502)
    assert 'agent 版本过旧' in old['error'] and (NS, 'a-one') in agent.clusters['beta-k']
    assert audits[-1][2]['success'] is False
    assert (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']['clusters'][0]['bound']
    outside = await call(client, 'POST', '/api/istio/live/delete',
                         {'group_id': a_id, 'k8s_name': 'prod-b', 'namespace': NS, 'name': 'b-one'}, status=400)
    assert '不在组' in outside['error']


@pytest.mark.asyncio
async def test_view_live_vs(env):
    """查看集群现状:实时取完整对象,去掉 managedFields 和 last-applied 注解;按集群读权限;旧版 agent 只有 spec。"""
    client, agent, _ = env
    group_id = await setup_group(client, agent)
    params = {'group_id': group_id, 'k8s_name': 'beta-k', 'namespace': NS, 'name': 'a'}
    data = (await call(client, 'GET', '/api/istio/live', **params))['data']
    meta = data['object']['metadata']
    assert data['full'] is True and meta['generation'] == 3 and meta['annotations'] == {'note': 'kept'}
    assert 'managedFields' not in meta and data['object']['spec'] == agent.clusters['beta-k'][(NS, 'a')]
    gone = await call(client, 'GET', '/api/istio/live', **{**params, 'name': 'gone'}, status=404)
    assert gone['error'] == f'集群 beta-k 里已经没有 {NS}/gone'
    outside = await call(client, 'GET', '/api/istio/live', **{**params, 'k8s_name': 'prod-x'}, status=400)
    assert '不在组' in outside['error']
    # 只能看有读权限的集群
    reader = {'X-Test-Role': 'none', 'X-Test-Clusters': json.dumps({'beta-k': 'read'})}
    await call(client, 'GET', '/api/istio/live', **params, headers=reader)
    denied = await call(client, 'GET', '/api/istio/live', **{**params, 'k8s_name': 'prod-a'}, headers=reader,
                        status=403)
    assert '没有集群 prod-a 的访问权限' in denied['error']
    # 旧版 agent 没有查看单个的接口:用列表接口拼出来,只有 spec
    agent.old_agent.add('beta-k')
    data = (await call(client, 'GET', '/api/istio/live', **params))['data']
    assert data['full'] is False and data['object']['metadata'] == {'name': 'a', 'namespace': NS, 'resourceVersion': '1'}
    assert data['object']['spec'] == agent.clusters['beta-k'][(NS, 'a')]
    await call(client, 'GET', '/api/istio/live', **{**params, 'name': 'gone'}, status=404)


async def backups(client, group_id, k8s_name, name='a', **kw):
    params = {'group_id': group_id, 'k8s_name': k8s_name, 'namespace': NS, 'name': name}
    return (await call(client, 'GET', '/api/istio/live/backups', **params, **kw))['data']


async def rollback(client, group_id, k8s_name, backup_id, name='a', status=200):
    return await call(client, 'POST', '/api/istio/live/rollback', {
        'group_id': group_id, 'k8s_name': k8s_name, 'namespace': NS, 'name': name, 'backup_id': backup_id},
        status=status)


async def cluster_row(client, vs_id, k8s_name):
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    return next(c for c in detail['clusters'] if c['k8s_name'] == k8s_name)


async def apply_to(client, vs_id, k8s_name):
    data = await call(client, 'POST', '/api/istio/apply', {'items': [{'vs_id': vs_id, 'k8s_name': k8s_name}]})
    return data['data'][0]


@pytest.mark.asyncio
async def test_domain_conflicts_in_pages_and_apply(env):
    """同一个集群里的域名冲突:矩阵和详情页按组标准要下发的内容查(集群里被忽略的、不在本组的 VS 也算),
    未纳管的按现状查;绑定的 gateway 不同的 host 不算。下发预览标出哪些是这次下发带来的,下发时有这样的就整批拒绝,
    原来就有的不拦;要挪的 host 两边一起下发就不冲突。"""
    client, agent, _ = env
    group_id = await setup_group(client, agent)
    agent.put('beta-k', 'c', vs(['a-beta-k.x.com'], [route('/c/', 3)]))  # 和 a 在 beta-k 的 host 重复
    agent.put('beta-k', 'e', vs(['a-beta-k.x.com'], [route('/e/', 5)], gateway='istio-system/internal-1080'))
    agent.put('prod-a', 'd', vs(['d.x.com'], [  # 两条规则都只按同一个域名匹配(authority / host 头各写一次)
        {'match': [{'authority': {'exact': 'd.x.com'}}], 'route': [{'destination': {'host': 'd1'}}]},
        {'match': [{'headers': {'host': {'exact': 'd.x.com'}}}], 'route': [{'destination': {'host': 'd2'}}]}]))
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'a', 'base': 'beta-k', 'clusters': ['beta-k', 'beta-p', 'prod-a']}]})
    a_id = imported['data'][0]['vs_id']
    a_issue = f'beta-k:host a-beta-k.x.com 也在 {NS}/c 里'
    d_issue = '域名 d.x.com:第 2 条(→ d2)被第 1 条(→ d1)挡住,不会生效'
    rows = await matrix(client, group_id)
    assert rows['a']['domain_issues'] == [a_issue] and rows['b']['domain_issues'] == []
    assert rows['c']['domain_issues'] == [f'beta-k:host a-beta-k.x.com 也在 {NS}/a 里']
    assert rows['e']['domain_issues'] == []  # gateway 不同
    assert rows['d']['domain_issues'] == [f'prod-a:{d_issue}']
    assert (await call(client, 'GET', '/api/istio/vs', vs_id=a_id))['data']['domain_issues'] == [a_issue]

    # 忽略 c:列表里不显示了,但它还在集群里,a 照样报
    await call(client, 'POST', '/api/istio/ignore', {'group_id': group_id, 'items': [{'namespace': NS, 'name': 'c'}]})
    rows = await matrix(client, group_id)
    assert 'c' not in rows and rows['a']['domain_issues'] == [a_issue]

    # 改了 a 在 beta-p 的 hosts(还没下发):按要下发的查,和 b 重复
    await call(client, 'PUT', '/api/istio/vs/cluster', {'vs_id': a_id, 'k8s_name': 'beta-p',
                                                       'hosts': ['a-beta.x.com', 'b-beta.x.com']})
    rows = await matrix(client, group_id)
    assert rows['a']['domain_issues'] == [a_issue, f'beta-p:host b-beta.x.com 也在 {NS}/b 里']
    assert rows['b']['domain_issues'] == [f'beta-p:host b-beta.x.com 也在 {NS}/a 里']
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=a_id))['data']
    assert detail['domain_issues'] == rows['a']['domain_issues']
    # 只能看 beta-k 的:只查 beta-k
    reader = {'X-Test-Role': 'none', 'X-Test-Clusters': json.dumps({'beta-k': 'read'})}
    assert (await matrix(client, group_id, headers=reader))['a']['domain_issues'] == [a_issue]

    # 下发预览:beta-k 的是集群里原来就有的,beta-p 的是这次下发带来的
    both = [{'vs_id': a_id, 'k8s_name': 'beta-k'}, {'vs_id': a_id, 'k8s_name': 'beta-p'}]
    preview = (await call(client, 'POST', '/api/istio/apply/preview', {'items': both}))['data']
    assert {p['k8s_name']: p['domain_issues'] for p in preview} == {
        'beta-k': [{'text': f'host a-beta-k.x.com 也在 {NS}/c 里', 'new': False}],
        'beta-p': [{'text': f'host b-beta.x.com 也在 {NS}/b 里', 'new': True}]}

    # 下发:有这次带来的冲突就整批拒绝,哪个集群都不改、也不备份;原来就有冲突的 beta-k 单独下发不拦
    rejected = await call(client, 'POST', '/api/istio/apply', {'items': both}, status=409)
    assert f'beta-p · {NS}/a:host b-beta.x.com 也在 {NS}/b 里' in rejected['error']
    assert agent.applied == [] and (await backups(client, group_id, 'beta-k'))['backups'] == []
    assert (await apply_to(client, a_id, 'beta-k'))['success']

    # 把 b-beta.x.com 从 b 挪到 a:两边一起下发就不冲突
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'b', 'base': 'beta-p', 'clusters': ['beta-p']}]})
    b_id = imported['data'][0]['vs_id']
    await call(client, 'PUT', '/api/istio/vs/cluster', {'vs_id': b_id, 'k8s_name': 'beta-p', 'hosts': ['b-new.x.com']})
    moved = [{'vs_id': a_id, 'k8s_name': 'beta-p'}, {'vs_id': b_id, 'k8s_name': 'beta-p'}]
    preview = (await call(client, 'POST', '/api/istio/apply/preview', {'items': moved}))['data']
    assert [p['domain_issues'] for p in preview] == [[], []]
    applied = await call(client, 'POST', '/api/istio/apply', {'items': moved})
    assert all(r['success'] for r in applied['data'])
    assert agent.clusters['beta-p'][(NS, 'a')]['hosts'] == ['a-beta.x.com', 'b-beta.x.com']
    assert agent.clusters['beta-p'][(NS, 'b')]['hosts'] == ['b-new.x.com']

    # 纳管 d:第 2 条被挡住是原来就有的,照样能下发;再加一条被挡住的规则就是这次带来的,下发被拒
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'd', 'base': 'prod-a', 'clusters': ['prod-a']}]})
    d_item = [{'vs_id': imported['data'][0]['vs_id'], 'k8s_name': 'prod-a'}]
    preview = (await call(client, 'POST', '/api/istio/apply/preview', {'items': d_item}))['data'][0]
    assert preview['same'] and preview['domain_issues'] == [{'text': d_issue, 'new': False}]
    assert (await call(client, 'POST', '/api/istio/apply', {'items': d_item}))['data'][0]['success']
    await call(client, 'POST', '/api/istio/httproute', {
        'match_rules': [{'$domains': True, 'uri': {'prefix': '/v2/'}}], 'forward_type': 'route',
        'forward_detail': [{'destination': {'host': 'd3', 'port': {'number': 80}}}],
        'domains': {'prod-a': [[{'field': 'authority', 'op': 'exact', 'value': 'd.x.com'}]]}},
        vs_id=d_item[0]['vs_id'])
    d3_issue = '域名 d.x.com:第 3 条(→ d3)被第 1 条(→ d1)挡住,不会生效'
    preview = (await call(client, 'POST', '/api/istio/apply/preview', {'items': d_item}))['data'][0]
    assert preview['domain_issues'] == [{'text': d_issue, 'new': False}, {'text': d3_issue, 'new': True}]
    rejected = await call(client, 'POST', '/api/istio/apply', {'items': d_item}, status=409)
    assert f'prod-a · {NS}/d:{d3_issue}' in rejected['error']
    assert (await matrix(client, group_id))['d']['domain_issues'] == [f'prod-a:{d_issue}', f'prod-a:{d3_issue}']


@pytest.mark.asyncio
async def test_backup_before_apply_and_rollback(env):
    """下发前备份集群里当时的内容(会变时才备份),每个集群每个 VS 留最近 5 份;回退写回备份,只改集群不改组标准,
    回退前也备份(可以撤销);agent 明确失败(集群没变)不留备份,结果不明时要回退到的那份先留着。"""
    client, agent, audits = env
    group_id = await setup_group(client, agent)
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'a', 'base': 'beta-k', 'clusters': ['beta-k', 'beta-p', 'prod-a']}]})
    a_id = imported['data'][0]['vs_id']
    before = copy.deepcopy(agent.clusters['beta-k'][(NS, 'a')])

    # 集群里的内容不会变的下发不备份
    assert (await apply_to(client, a_id, 'beta-k'))['success']
    assert (await backups(client, group_id, 'beta-k'))['backups'] == []

    # 加一条规则再下发:先备份下发前的内容
    await call(client, 'POST', '/api/istio/httproute', {
        'match_rules': [{'uri': {'prefix': '/new/'}}], 'forward_type': 'route',
        'forward_detail': [{'destination': {'host': 'new', 'port': {'number': 80}}}]}, vs_id=a_id)
    assert (await apply_to(client, a_id, 'beta-k'))['success']
    data = await backups(client, group_id, 'beta-k')
    assert data['keep'] == 5 and data['live_error'] is None and len(data['live']['spec']['http']) == 3
    [first] = data['backups']
    assert first['action'] == 'apply' and first['created_by'] == 'admin' and first['resource_version'] == '1'
    assert first['spec'] == before and first['exists'] and not first['same']
    row = await cluster_row(client, a_id, 'beta-k')
    assert row['backup_count'] == 1 and row['rolled_back'] is None and row['status'] == S.SYNCED
    assert (await cluster_row(client, a_id, 'beta-p'))['backup_count'] == 0

    # 回退:写回下发前的内容,组标准不变(状态不一致);回退前的内容也备份了,审计里记着
    rolled = await rollback(client, group_id, 'beta-k', first['id'])
    assert rolled['msg'].startswith('已把集群 beta-k 里的 istio-system/a 回退到 20')
    assert rolled['msg'].endswith(' 下发前的内容;组标准没有变,下次下发会再用组标准覆盖')
    assert agent.clusters['beta-k'][(NS, 'a')] == before
    assert agent.applied[-1][1] == {'apiVersion': S.API_VERSION, 'kind': 'VirtualService',
                                    'metadata': {'name': 'a', 'namespace': NS}, 'spec': before}
    _, action, record = audits[-1]
    assert action == 'istio.rollback' and record['success'] and record['env'] == 'beta-k'
    assert record['detail']['backup_id'] == first['id'] and len(record['detail']['spec']['http']) == 3
    data = await backups(client, group_id, 'beta-k')
    assert [(b['action'], b['same']) for b in data['backups']] == [('rollback', False), ('apply', True)]
    row = await cluster_row(client, a_id, 'beta-k')
    assert row['status'] == S.DIFF and row['backup_count'] == 2 and row['rolled_back']['by'] == 'admin'
    assert len((await call(client, 'GET', '/api/istio/vs', vs_id=a_id))['data']['routes']) == 3

    # 已经一样了:不用回退,也不再备份
    same = await rollback(client, group_id, 'beta-k', first['id'])
    assert '不需要回退' in same['msg'] and len((await backups(client, group_id, 'beta-k'))['backups']) == 2

    # 撤销回退:回到回退前的内容,也就是最近一次下发的,不再显示回退过
    await rollback(client, group_id, 'beta-k', data['backups'][0]['id'])
    assert len(agent.clusters['beta-k'][(NS, 'a')]['http']) == 3
    row = await cluster_row(client, a_id, 'beta-k')
    assert row['status'] == S.SYNCED and row['rolled_back'] is None and row['backup_count'] == 3
    assert (await apply_to(client, a_id, 'beta-k'))['success']
    row = await cluster_row(client, a_id, 'beta-k')
    assert row['rolled_back'] is None and row['backup_count'] == 3

    # agent 明确回了失败(集群没变):不留备份
    await call(client, 'PUT', '/api/istio/vs/cluster', {'vs_id': a_id, 'k8s_name': 'beta-p',
                                                       'hosts': ['reject.example.com']})
    assert not (await apply_to(client, a_id, 'beta-p'))['success']
    assert (await backups(client, group_id, 'beta-p'))['backups'] == []

    # 每个集群每个 VS 只留最近 5 份,新的在前
    for n in range(6):
        await call(client, 'PUT', '/api/istio/vs', {'gateways': [f'istio-system/gw{n}']}, vs_id=a_id)
        assert (await apply_to(client, a_id, 'prod-a'))['success']
    data = await backups(client, group_id, 'prod-a')
    assert [b['spec']['gateways'] for b in data['backups']] == [[f'istio-system/gw{n}'] for n in (4, 3, 2, 1, 0)]
    assert (await cluster_row(client, a_id, 'prod-a'))['backup_count'] == 5

    # 回退到最早那份时等不到回应(没生效):这份先留着,还能再回退;成功后恢复成 5 份
    oldest = data['backups'][-1]
    agent.lost.add('prod-a')
    lost = await rollback(client, group_id, 'prod-a', oldest['id'], status=502)
    assert lost['error'].startswith('回退失败:等待 prod-a') and audits[-1][2]['success'] is False
    agent.lost.clear()
    data = await backups(client, group_id, 'prod-a')
    assert len(data['backups']) == 6 and data['backups'][-1]['id'] == oldest['id']
    assert data['backups'][0]['action'] == 'rollback'
    await rollback(client, group_id, 'prod-a', oldest['id'])
    assert agent.clusters['prod-a'][(NS, 'a')]['gateways'] == ['istio-system/gw0']
    data = await backups(client, group_id, 'prod-a')
    assert len(data['backups']) == 5 and oldest['id'] not in {b['id'] for b in data['backups']}


@pytest.mark.asyncio
async def test_rollback_create_delete_and_limits(env):
    """第一次下发前集群里没有:回退就是删除;删除前也备份,回退可以恢复。只能看有读权限的集群的备份;
    取不到集群现状时备份照样列出、不下发;旧版 agent 删不了;集群移出所有组时备份一并删除。"""
    client, agent, audits = env
    group_id = await setup_group(client, agent)
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    created = await call(client, 'POST', '/api/istio/vs', {
        'group_id': group_id, 'namespace': NS, 'name': 'fresh',
        'clusters': [{'k8s_name': 'beta-k', 'hosts': ['fresh.x.com']}]})
    fresh_id = created['data']['id']

    # 第一次下发:备份里记着集群里没有,回退就是从集群删除(组标准还部署到该集群,状态是集群中没有)
    assert (await apply_to(client, fresh_id, 'beta-k'))['success']
    [made] = (await backups(client, group_id, 'beta-k', name='fresh'))['backups']
    assert made['spec'] is None and not made['exists'] and not made['same'] and made['resource_version'] == ''
    gone = await rollback(client, group_id, 'beta-k', made['id'], name='fresh')
    assert ' 下发前集群 beta-k 里还没有 istio-system/fresh,已从集群删除;组标准没有变' in gone['msg']
    assert (NS, 'fresh') not in agent.clusters['beta-k'] and agent.deleted[-1] == ('beta-k', (NS, 'fresh'))
    row = await cluster_row(client, fresh_id, 'beta-k')
    assert row['bound'] and row['status'] == S.MISSING and row['rolled_back']['by'] == 'admin'
    data = await backups(client, group_id, 'beta-k', name='fresh')
    assert data['live'] == {'exists': False, 'spec': None, 'resource_version': ''}
    assert [(b['action'], b['exists'], b['same']) for b in data['backups']] == [
        ('rollback', True, False), ('apply', False, True)]

    # 旧版 agent 没有删除接口:回退到「没有」失败、提示升级,回退前的备份不留
    assert (await apply_to(client, fresh_id, 'beta-k'))['success']
    agent.old_agent.add('beta-k')
    old = await rollback(client, group_id, 'beta-k', made['id'], name='fresh', status=502)
    assert 'agent 版本过旧' in old['error'] and (NS, 'fresh') in agent.clusters['beta-k']
    assert len((await backups(client, group_id, 'beta-k', name='fresh'))['backups']) == 3
    agent.old_agent.clear()

    # 删除前也备份,误删可以回退恢复(没有组标准的也行)
    await call(client, 'POST', '/api/istio/live/delete', {'group_id': group_id, 'k8s_name': 'beta-p',
                                                         'namespace': NS, 'name': 'b'})
    [removed] = (await backups(client, group_id, 'beta-p', name='b'))['backups']
    assert removed['action'] == 'delete' and removed['spec']['hosts'] == ['b-beta.x.com']
    restored = await rollback(client, group_id, 'beta-p', removed['id'], name='b')
    assert restored['msg'].endswith(' 删除前的内容') and agent.clusters['beta-p'][(NS, 'b')] == removed['spec']

    # 备份不是这个 VS 的
    wrong = await rollback(client, group_id, 'beta-k', removed['id'], status=404)
    assert '这份备份已经没有了' in wrong['error']

    # 只能看有读权限的集群的备份
    reader = {'X-Test-Role': 'none', 'X-Test-Clusters': json.dumps({'beta-k': 'read'})}
    assert (await backups(client, group_id, 'beta-k', name='fresh', headers=reader))['backups']
    denied = await call(client, 'GET', '/api/istio/live/backups', group_id=group_id, k8s_name='beta-p',
                        namespace=NS, name='b', headers=reader, status=403)
    assert '没有集群 beta-p 的访问权限' in denied['error']

    # 取不到集群现状:备份照样列出,只是没法比较;也备份不了,不下发
    agent.offline.add('beta-k')
    data = await backups(client, group_id, 'beta-k', name='fresh')
    assert data['live'] is None and '不在线' in data['live_error'] and not any(b['same'] for b in data['backups'])
    result = await apply_to(client, fresh_id, 'beta-k')
    assert not result['success'] and result['error'].startswith('下发前取集群现状(用于备份)失败,没有下发:')
    assert audits[-1][1] == 'istio.apply' and audits[-1][2]['success'] is False
    agent.offline.clear()

    # 集群移出所有组:它的备份一并删除
    await call(client, 'PUT', '/api/istio/groups', {
        'name': 'cassmall', 'description': '双活',
        'clusters': [{'k8s_name': 'beta-k', 'env_type': 'test'}, {'k8s_name': 'prod-a', 'env_type': 'prod'}]},
        id=group_id)
    count = 'SELECT count(*) FROM istio_vs_backup WHERE k8s_name = $1'
    assert await db.pg_fetchval(count, 'beta-p') == 0 and await db.pg_fetchval(count, 'beta-k') == 3


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
        return vs([*hosts, f'admin-{site}.x.com'], [host_rule, route('/api/', 6001)])

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
    # (生产用单独的 admin 域名:svc-* 的请求都被第 1 条接走了,这条排在后面不会生效,下发会被拒)
    rule = {'match_rules': [{'uri': {'prefix': '/admin/'}, S.DOMAIN_KEY: True}], 'forward_type': 'route',
            'forward_detail': [{'destination': {'host': 'admin.b2c.svc.cluster.local'}}],
            'domains': {'beta-k': [[{'value': 'svc-beta.x.com'}]], 'prod-p': [[{'value': 'admin-p.x.com'}]]}}
    path = f'/api/istio/httproute?vs_id={vs_id}'
    assert '请设置集群 prod-k 的域名' in (await call(client, 'POST', path, rule, status=400))['error']
    rule['domains']['prod-k'] = [[]]
    assert '一个域名都没选' in (await call(client, 'POST', path, rule, status=400))['error']
    rule['domains']['prod-k'] = [[{'value': 'admin-k.x.com'}, {'value': ' admin-k.x.com '}]]  # 重复的去掉
    rule['domains']['prod-x'] = [[{'value': 'svc-x.x.com'}]]
    assert '没有部署到集群 prod-x' in (await call(client, 'POST', path, rule, status=400))['error']
    rule['domains'].pop('prod-x')
    await call(client, 'POST', path, rule)
    assert set(cells((await matrix(client, group_id))['svc']).values()) == {S.PENDING, S.NONE}

    # 下发到 prod-k:用的是 prod-k 自己的域名
    await call(client, 'POST', '/api/istio/apply', {'items': [{'vs_id': vs_id, 'k8s_name': 'prod-k'}]})
    env_name, body = agent.applied[-1]
    http = body['spec']['http']
    assert env_name == 'prod-k' and body['spec']['hosts'] == ['svc-k.x.com', 'svc.x.com', 'admin-k.x.com']
    assert http[0]['match'] == [{'authority': {'exact': 'svc-k.x.com'}}, {'authority': {'exact': 'svc.x.com'}}]
    assert http[2]['match'] == [{'uri': {'prefix': '/admin/'}, 'authority': {'exact': 'admin-k.x.com'}}]
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


@pytest.mark.asyncio
async def test_import_takes_domains_from_rule_with_same_target(env):
    """只有域名条件的几条规则,测试集群少一条:域名取转发目标一样的规则的,不按位置错位到别的规则上。"""
    client, agent, _ = env

    def host_rule(service, port, *domains):
        return {'match': [{'authority': {'exact': d}} for d in domains],
                'route': [{'destination': {'host': f'{service}.adv.svc.cluster.local', 'port': {'number': port}}}]}

    agent.put('prod-k', 'adv', vs(['cap.x.com', 'job.x.com', 'nacos.x.com'], [
        host_rule('gateway', 10001, 'cap.x.com'), host_rule('job', 10089, 'job.x.com'),
        host_rule('nacos', 8848, 'nacos.x.com')]))
    agent.put('beta-k', 'adv', vs(['cap-beta.x.com', 'nacos-alpha.x.com'], [
        host_rule('gateway', 10001, 'cap-beta.x.com'), host_rule('nacos', 8848, 'nacos-alpha.x.com')]))
    created = await call(client, 'POST', '/api/istio/groups', {'name': 'g', 'clusters': [
        {'k8s_name': 'beta-k', 'env_type': 'test'}, {'k8s_name': 'prod-k', 'env_type': 'prod'}]})
    group_id = created['data']['id']
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'adv', 'base': 'prod-k', 'clusters': ['beta-k', 'prod-k']}]})
    vs_id = (await matrix(client, group_id))['adv']['vs_id']
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    gateway, job, nacos = detail['routes']
    assert domain_values(gateway['domains']['beta-k']) == [['cap-beta.x.com']]
    assert 'beta-k' not in job['domains']
    assert domain_values(nacos['domains']['beta-k']) == [['nacos-alpha.x.com']]
    beta = next(c for c in detail['clusters'] if c['k8s_name'] == 'beta-k')
    assert beta['status'] == S.INCOMPLETE and beta['missing_domains'] == ['第 2 条规则(域名按集群 → job.adv)']
    # 「编辑 hosts 和域名」的默认值同样按转发目标取:job 规则在测试集群里没有对应的
    assert set(beta['live_domains']) == {str(gateway['id']), str(nacos['id'])}


@pytest.mark.asyncio
async def test_bind_clusters_that_already_have_the_vs(env):
    """新集群加入组后,集群里已有、组标准还没部署到的 VS 显示「未纳入」:批量纳入后 hosts 和规则里的域名取各自的现状,
    现状记为「上次下发」,规则一样的一致、不一样的待下发;集群里没有的、已经纳入的、被忽略的不能纳入。只改花折里的数据。"""
    client, agent, audits = env
    group_id = await setup_group(client, agent)
    for k8s_name in ('beta-k', 'prod-a'):  # 只按域名转发,各集群只差域名
        agent.put(k8s_name, 'd', vs([f'd-{k8s_name}.x.com'], [
            {'match': [{'authority': {'exact': f'd-{k8s_name}.x.com'}}], 'route': [{'destination': {'host': 'd'}}]}]))
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    imported = await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': name, 'base': 'beta-k', 'clusters': ['beta-k']} for name in ('a', 'b', 'd')]})
    a_id, b_id, d_id = (r['vs_id'] for r in imported['data'])
    rows = await matrix(client, group_id)
    assert cells(rows['a']) == {'beta-k': S.SYNCED, 'beta-p': S.UNBOUND, 'prod-a': S.UNBOUND}
    assert cells(rows['b']) == {'beta-k': S.SYNCED, 'beta-p': S.UNBOUND, 'prod-a': S.NONE}

    bound = await call(client, 'POST', '/api/istio/bind', {'group_id': group_id, 'items': [
        {'vs_id': a_id, 'k8s_name': 'beta-p'}, {'vs_id': a_id, 'k8s_name': 'prod-a'},
        {'vs_id': b_id, 'k8s_name': 'beta-p'}, {'vs_id': b_id, 'k8s_name': 'prod-a'},
        {'vs_id': d_id, 'k8s_name': 'prod-a'}, {'vs_id': a_id, 'k8s_name': 'beta-k'},
        {'vs_id': a_id, 'k8s_name': 'prod-a'}, {'vs_id': a_id, 'k8s_name': 'prod-x'}]})
    assert [(r['name'], r['k8s_name'], r['success'], r.get('status') or r['error']) for r in bound['data']] == [
        ('a', 'beta-p', True, S.SYNCED), ('a', 'prod-a', True, S.SYNCED), ('b', 'beta-p', True, S.PENDING),
        ('b', 'prod-a', False, '集群 prod-a 的现状里没有这个 VirtualService,请先刷新现状'),
        ('d', 'prod-a', True, S.SYNCED), ('a', 'beta-k', False, '已经部署到集群 beta-k'),
        ('a', 'prod-a', False, '已经部署到集群 prod-a'), ('a', 'prod-x', False, '集群 prod-x 不在组 cassmall 里')]
    rows = await matrix(client, group_id)
    assert cells(rows['a']) == {'beta-k': S.SYNCED, 'beta-p': S.SYNCED, 'prod-a': S.SYNCED}
    assert cells(rows['b']) == {'beta-k': S.SYNCED, 'beta-p': S.PENDING, 'prod-a': S.NONE}
    assert rows['a']['cells']['prod-a']['hosts'] == ['a.x.com']  # hosts 取各自的现状
    d_rule = (await call(client, 'GET', '/api/istio/vs', vs_id=d_id))['data']['routes'][0]
    assert {k: domain_values(v) for k, v in d_rule['domains'].items()} == {
        'beta-k': [['d-beta-k.x.com']], 'prod-a': [['d-prod-a.x.com']]}
    assert agent.applied == [] and audits == []
    # 待下发的看过差异再下发:下发前取的现状就是纳入时记下的,备份后用组标准覆盖
    assert (await apply_to(client, b_id, 'beta-p'))['success']
    assert len(agent.clusters['beta-p'][(NS, 'b')]['http']) == 2

    # 「部署到此集群」纳入集群里已有的(规则不一样):同样记下现状,显示待下发而不是不一致
    agent.put('beta-p', 'c', vs(['c-p.x.com'], [route('/c1/', 1)]))
    agent.put('beta-k', 'c', vs(['c-k.x.com'], [route('/c1/', 1), route('/c2/', 2)]))
    agent.put('prod-a', 'c', vs(['c.x.com'], [route('/c1/', 1)]))
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    c_id = (await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'c', 'base': 'beta-k', 'clusters': ['beta-k']}]}))['data'][0]['vs_id']
    await call(client, 'PUT', '/api/istio/vs/cluster', {'vs_id': c_id, 'k8s_name': 'beta-p', 'hosts': ['c-p.x.com']})
    assert cells((await matrix(client, group_id))['c'])['beta-p'] == S.PENDING
    # 被忽略的不能纳入(列表里也不显示)
    await call(client, 'POST', '/api/istio/ignore', {'group_id': group_id, 'items': [{'namespace': NS, 'name': 'c'}]})
    ignored = await call(client, 'POST', '/api/istio/bind', {'group_id': group_id, 'items': [
        {'vs_id': c_id, 'k8s_name': 'prod-a'}]})
    assert ignored['data'][0]['error'] == '已在本组手动忽略,不能纳入;如需由本组管理,先在「查看忽略」里取消忽略'
    await call(client, 'POST', '/api/istio/bind', {'group_id': group_id, 'items': []}, status=400)


@pytest.mark.asyncio
async def test_sync_rules_from_cluster_to_standard(env):
    """对比里从集群现状整条同步规则到组标准:预览不保存;保存后其他集群保持原来的域名,新加的规则缺少域名;
    打开对比之后组标准变了就拒绝;也能按现状删掉组标准里的规则。"""
    client, agent, _ = env

    def host_rule(service, domain, timeout=None):
        entry = {'match': [{'authority': {'exact': domain}}],
                 'route': [{'destination': {'host': f'{service}.b2c.svc.cluster.local'}}]}
        return {**entry, 'timeout': timeout} if timeout else entry

    def api(timeout):
        return {**route('/api/', 6001), 'timeout': timeout}

    agent.put('prod-k', 'svc', vs(['svc.x.com'], [host_rule('svc', 'svc.x.com'), api('7s')]))
    agent.put('beta-k', 'svc', vs(['svc-beta.x.com', 'admin-beta.x.com'], [
        host_rule('svc', 'svc-beta.x.com', '3s'), host_rule('admin', 'admin-beta.x.com'), api('9s'), route('/new/', 7001)]))
    created = await call(client, 'POST', '/api/istio/groups', {'name': 'g', 'clusters': [
        {'k8s_name': 'beta-k', 'env_type': 'test'}, {'k8s_name': 'prod-k', 'env_type': 'prod'}]})
    group_id = created['data']['id']
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'svc', 'base': 'prod-k', 'clusters': ['beta-k', 'prod-k']}]})
    vs_id = (await matrix(client, group_id))['svc']['vs_id']

    async def compare(k8s_name):
        data = (await call(client, 'GET', '/api/istio/compare', group_id=group_id, namespace=NS, name='svc',
                           left='standard', right=f'cluster@{k8s_name}'))['data']
        body = {'group_id': group_id, 'namespace': NS, 'name': 'svc', 'standard': 'standard', 'k8s_name': k8s_name,
                'standard_version': data['left']['version'], 'live_version': data['right']['version']}
        return [(r['op'], r['left_index'], r['right_index']) for r in data['diff']['routes']], body

    rows, body = await compare('beta-k')
    assert rows == [('changed', 0, 0), ('right_only', None, 1), ('changed', 1, 2), ('right_only', None, 3)]
    # 同步改了超时的按域名转发规则、新加的两条;/api/ 的超时不同步
    body['rules'] = [{'standard_index': i, 'live_index': j} for _, i, j in (rows[0], rows[1], rows[3])]
    preview = (await call(client, 'POST', '/api/istio/vs/sync/preview', body))['data']
    assert (preview['summary']['added'], preview['summary']['replaced']) == (2, 1)
    assert preview['missing_domains'] == [{'k8s_name': 'prod-k', 'rules': ['第 2 条规则(域名按集群 → admin.b2c)']}]
    assert len(preview['spec']['http']) == 4
    assert len((await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']['routes']) == 2  # 预览不保存

    saved = await call(client, 'POST', '/api/istio/vs/sync', body)
    assert saved['msg'] == '已从 beta-k 的现状同步到组标准:加入 2 条规则、替换 1 条规则,下发后生效'
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    svc, admin, api_rule, new = detail['routes']
    assert (svc['timeout'], api_rule['timeout'], new['match_rules']) == ('3s', '7s', [{'uri': {'prefix': '/new/'}}])
    assert domain_values(svc['domains']['prod-k']) == [['svc.x.com']]  # 其他集群保持原来的域名
    assert domain_values(svc['domains']['beta-k']) == [['svc-beta.x.com']]
    assert domain_values(admin['domains']['beta-k']) == [['admin-beta.x.com']] and 'prod-k' not in admin['domains']
    status = {c['k8s_name']: c['status'] for c in detail['clusters']}
    assert status == {'beta-k': S.PENDING, 'prod-k': S.INCOMPLETE}
    # 组标准已经变了,还用打开对比时的版本:拒绝
    assert '刷新对比' in (await call(client, 'POST', '/api/istio/vs/sync', body, status=409))['error']

    # 按 prod-k 的现状删掉组标准里的 /new/
    rows, body = await compare('prod-k')
    assert rows == [('changed', 0, 0), ('left_only', 1, None), ('same', 2, 1), ('left_only', 3, None)]
    bad = await call(client, 'POST', '/api/istio/vs/sync/preview',
                     {**body, 'rules': [{'standard_index': 3, 'live_index': 1}]}, status=409)
    assert '对不上' in bad['error']
    assert '没有要同步的内容' in (await call(client, 'POST', '/api/istio/vs/sync', body, status=400))['error']
    saved = await call(client, 'POST', '/api/istio/vs/sync', {**body, 'rules': [{'standard_index': 3}]})
    assert '删除 1 条规则' in saved['msg']
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=vs_id))['data']
    assert [r['match_rules'][0].get('uri') for r in detail['routes']] == [None, None, {'prefix': '/api/'}]
    # 组标准一侧只能是组标准;集群里没有这个 VS 的不能同步
    wrong = await call(client, 'POST', '/api/istio/vs/sync', {**body, 'standard': 'cluster@beta-k'}, status=400)
    assert 'standard 只能是' in wrong['error']
    agent.clusters['prod-k'].clear()
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    gone = await call(client, 'POST', '/api/istio/vs/sync', {**body, 'rules': [{'standard_index': 0}]}, status=404)
    assert '现状里没有' in gone['error']


def as_user(role, clusters=None):
    return {'X-Test-Role': role, 'X-Test-Clusters': json.dumps(clusters or {})}


@pytest.mark.asyncio
async def test_istio_permissions_by_cluster(env, monkeypatch):
    """修改要「Istio 修改」(默认只有管理员列):组级修改看组内每个集群,集群级修改只看目标集群。"""
    client, agent, _ = env
    import utils

    async def names():
        return ['beta-k', 'beta-p', 'prod-a', 'prod-b']

    monkeypatch.setattr(utils, 'get_k8s_names', names)
    group_id = await setup_group(client, agent)
    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id})
    await call(client, 'POST', '/api/istio/import', {'group_id': group_id, 'items': [
        {'namespace': NS, 'name': 'a', 'base': 'beta-k', 'clusters': ['beta-k', 'beta-p', 'prod-a']}]})
    a_id = (await matrix(client, group_id))['a']['vs_id']

    # 只在 beta-k 是管理员:能刷新、预览、下发 beta-k;组级修改和别的集群都不行
    one = as_user('none', {'beta-k': 'admin', 'beta-p': 'read', 'prod-a': 'rw'})
    groups = (await call(client, 'GET', '/api/istio/groups', headers=one))['data']
    group = groups['groups'][0]
    assert group['can_manage'] is False
    assert {c['k8s_name']: c['can_istio'] for c in group['clusters']} == {'beta-k': True, 'beta-p': False, 'prod-a': False}
    assert [(c['k8s_name'], c['can_istio']) for c in groups['clusters']] == [
        ('beta-k', True), ('beta-p', False), ('prod-a', False)]  # 看不到的 prod-b 不列
    data = (await call(client, 'GET', '/api/istio/matrix', group_id=group_id, headers=one))['data']
    assert data['group']['can_manage'] is False and data['group']['clusters'][0]['can_istio'] is True
    detail = (await call(client, 'GET', '/api/istio/vs', vs_id=a_id, headers=one))['data']
    assert detail['can_manage'] is False
    assert {c['k8s_name']: c['can_istio'] for c in detail['clusters']} == {'beta-k': True, 'beta-p': False, 'prod-a': False}

    await call(client, 'POST', '/api/istio/collect', {'group_id': group_id, 'k8s_names': ['beta-k']}, headers=one)
    denied = await call(client, 'POST', '/api/istio/collect', {'group_id': group_id}, headers=one, status=403)
    assert denied['error'] == '当前账号在集群 beta-p 是「只读」权限、在集群 prod-a 是「读写」权限,不能执行「Istio 修改」'
    preview = await call(client, 'POST', '/api/istio/apply/preview', {'items': [{'vs_id': a_id, 'k8s_name': 'beta-k'}]},
                         headers=one)
    assert preview['data'][0]['error'] is None
    await call(client, 'POST', '/api/istio/apply', {'items': [{'vs_id': a_id, 'k8s_name': 'beta-k'}]}, headers=one)
    for path, body in (('/api/istio/apply/preview', {'items': [{'vs_id': a_id, 'k8s_name': 'prod-a'}]}),
                       ('/api/istio/apply', {'items': [{'vs_id': a_id, 'k8s_name': 'beta-k'},
                                                       {'vs_id': a_id, 'k8s_name': 'beta-p'}]}),
                       ('/api/istio/live/delete', {'group_id': group_id, 'k8s_name': 'beta-p', 'namespace': NS, 'name': 'b'}),
                       ('/api/istio/live/rollback', {'group_id': group_id, 'k8s_name': 'prod-a', 'namespace': NS,
                                                     'name': 'a', 'backup_id': 1})):
        await call(client, 'POST', path, body, headers=one, status=403)
    applied = len(agent.applied)
    # 组级修改:组内每个集群都要有
    for method, path, body, params in (
            ('POST', '/api/istio/import', {'group_id': group_id, 'items': [
                {'namespace': NS, 'name': 'b', 'base': 'beta-k', 'clusters': ['beta-k']}]}, {}),
            ('POST', '/api/istio/bind', {'group_id': group_id, 'items': [{'vs_id': a_id, 'k8s_name': 'beta-k'}]}, {}),
            ('POST', '/api/istio/ignore', {'group_id': group_id, 'items': [{'namespace': NS, 'name': 'b'}]}, {}),
            ('POST', '/api/istio/vs/reset', {'vs_id': a_id, 'k8s_name': 'beta-k'}, {}),
            ('PUT', '/api/istio/vs', {'gateways': ['istio-system/x']}, {'vs_id': a_id}),
            ('PUT', '/api/istio/vs/cluster', {'vs_id': a_id, 'k8s_name': 'beta-k', 'hosts': ['x.com']}, {}),
            ('DELETE', '/api/istio/vs/cluster', None, {'vs_id': a_id, 'k8s_name': 'beta-k'}),
            ('POST', '/api/istio/httproute', {'match_rules': [{'uri': {'prefix': '/x/'}}], 'forward_type': 'route',
                                              'forward_detail': [{'destination': {'host': 'x'}}]}, {'vs_id': a_id}),
            ('POST', '/api/istio/httproute/reorder', None, {'vs_id': a_id}),
            ('DELETE', '/api/istio/vs', None, {'vs_id': a_id}),
            ('DELETE', '/api/istio/groups', None, {'id': group_id})):
        denied = await call(client, method, path, body, headers=one, status=403, **params)
        assert '组级修改要求组内每个集群都有该权限' in denied['error']
    assert len(agent.applied) == applied and (await matrix(client, group_id))['a']['vs_id'] == a_id

    # 集群管理员(全局角色):单独降为读写的集群算没有;改组成员时新旧成员都算
    manager = as_user('cluster_admin', {'prod-b': 'rw'})
    created = await call(client, 'POST', '/api/istio/groups', {'name': 'mine', 'clusters': [{'k8s_name': 'beta-k'}]},
                         headers=manager)
    mine = created['data']['id']
    denied = await call(client, 'PUT', f'/api/istio/groups?id={mine}', {
        'name': 'mine', 'clusters': [{'k8s_name': 'beta-k'}, {'k8s_name': 'prod-b', 'env_type': 'prod'}]},
        headers=manager, status=403)
    assert 'prod-b 是「读写」权限' in denied['error']
    await call(client, 'POST', '/api/istio/groups', {'name': 'x', 'clusters': [{'k8s_name': 'prod-b'}]},
               headers=manager, status=403)
    # 组里有看不到的集群:不报它的名字
    hidden = await call(client, 'POST', '/api/istio/ignore', {'group_id': group_id, 'items': [{'namespace': NS, 'name': 'b'}]},
                        headers=as_user('none', {'beta-k': 'admin', 'beta-p': 'admin'}), status=403)
    assert hidden['error'].startswith('当前账号没有其中 1 个集群的访问权限,') and 'prod-a' not in hidden['error']
    assert (await call(client, 'GET', '/api/istio/vs', vs_id=a_id, headers=manager))['data']['can_manage'] is True
    await call(client, 'POST', '/api/istio/ignore', {'group_id': group_id, 'items': [{'namespace': NS, 'name': 'b'}]},
               headers=manager)
    # 没有集群的组按全局角色:集群管理员能看到也能管理,读写看不到
    empty = (await call(client, 'POST', '/api/istio/groups', {'name': 'empty'}, headers=manager))['data']['id']
    listed = {g['id']: g for g in (await call(client, 'GET', '/api/istio/groups', headers=manager))['data']['groups']}
    assert listed[empty]['can_manage'] is True and listed[mine]['can_manage'] is True
    rw = as_user('rw')
    listed = {g['id']: g for g in (await call(client, 'GET', '/api/istio/groups', headers=rw))['data']['groups']}
    assert empty not in listed and listed[group_id]['can_manage'] is False
    await call(client, 'POST', '/api/istio/unignore', {'group_id': group_id, 'items': [{'namespace': NS, 'name': 'b'}]},
               headers=rw, status=403)

    # 「权限与审批」里给读写列勾上 Istio 修改(写库,下次读库生效)
    await permissions.save_policy({'network.istio': {'read': False, 'rw': True, 'admin': True, 'approval': True}},
                                  permissions.default_commands(), 'admin')
    permissions.invalidate_cache()
    assert (await permissions.load_policy())['ops']['network.istio']['rw'] is True
    await call(client, 'POST', '/api/istio/unignore', {'group_id': group_id, 'items': [{'namespace': NS, 'name': 'b'}]},
               headers=rw)
    listed = {g['id']: g for g in (await call(client, 'GET', '/api/istio/groups', headers=rw))['data']['groups']}
    assert listed[group_id]['can_manage'] is True and listed[empty]['can_manage'] is True
