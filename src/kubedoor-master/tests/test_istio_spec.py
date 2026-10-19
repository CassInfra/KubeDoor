"""istio_route/spec.py 的纯逻辑测试:采集 → 下发无损、归一化、规则对齐对比、名称规范、状态与组矩阵。"""
import copy
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from istio_route import spec as S  # noqa: E402


def route(prefix, port, name=None, rewrite=None, timeout='7s', **extra):
    entry = {}
    if name:
        entry['name'] = name
    entry['match'] = [{'uri': {'prefix': prefix}}]
    if rewrite:
        entry['rewrite'] = {'uri': rewrite}
    entry['route'] = [{'destination': {'host': f'svc{port}.ns.svc.cluster.local', 'port': {'number': port}}}]
    if timeout:
        entry['timeout'] = timeout
    entry.update(extra)
    return entry


DEFAULT = {
    'route': [{'destination': {'host': 'web.ns.svc.cluster.local', 'port': {'number': 80}},
               'headers': {'request': {'set': {'X-Forwarded-Proto': 'https'}}}}],
    'timeout': '30s',
}


def vs_spec(hosts=('a.example.com',), routes=None, default=DEFAULT, **extra):
    http = list(routes if routes is not None else [
        route('/user-service/', 6602, name='/user-service', rewrite='/'),
        route('/user-api/', 6001),
    ])
    if default:
        http.append(copy.deepcopy(default))
    spec = {'hosts': list(hosts), 'gateways': ['istio-system/internal'], 'http': http}
    spec.update(extra)
    return spec


# ---------------------------------------------------------------- 解析 / 渲染
def test_parse_splits_shared_parts_hosts_and_default_route():
    parsed = S.parse_spec(vs_spec())
    assert parsed['hosts'] == ['a.example.com']
    assert parsed['vs']['gateways'] == ['istio-system/internal']
    assert parsed['vs']['df_forward_type'] == 'route' and parsed['vs']['df_forward_timeout'] == '30s'
    assert parsed['vs']['df_extra'] is None and parsed['vs']['extra_spec'] is None
    assert [r['priority'] for r in parsed['routes']] == [10, 20]
    first, second = parsed['routes']
    assert first['name'] == '/user-service' and first['rewrite_rules'] == {'uri': '/'}
    assert second['name'] is None and second['rewrite_rules'] is None and second['extra'] is None


def test_round_trip_keeps_fields_that_are_not_modelled():
    """采集到的字段原样保存、原样渲染:下发组标准不会冲掉 retries、corsPolicy、redirect 这类配置。"""
    spec = vs_spec(
        routes=[
            route('/a/', 1, name='/a', rewrite='/', retries={'attempts': 3, 'perTryTimeout': '2s'}),
            {'match': [{'uri': {'exact': '/old'}}], 'redirect': {'uri': '/new'}},  # 没有 route / delegate
            {'route': [{'destination': {'host': 'mid'}}]},  # 中间没有 match 的规则保持原位
            route('/b/', 2, corsPolicy={'allowOrigins': [{'exact': '*'}]}),
        ],
        default={'redirect': {'uri': '/index'}, 'name': 'fallback'},
        exportTo=['.'],
        tcp=[{'route': [{'destination': {'host': 'db'}}]}],
    )
    parsed = S.parse_spec(spec)
    routes = parsed['routes']
    assert routes[0]['extra'] == {'retries': {'attempts': 3, 'perTryTimeout': '2s'}}
    assert routes[1]['forward_type'] is None and routes[1]['extra'] == {'redirect': {'uri': '/new'}}
    assert routes[2]['match_rules'] is None and routes[2]['forward_type'] == 'route'
    assert parsed['vs']['df_forward_type'] is None
    assert parsed['vs']['df_extra'] == {'redirect': {'uri': '/index'}, 'name': 'fallback'}
    assert parsed['vs']['extra_spec'] == {'exportTo': ['.'], 'tcp': spec['tcp']}

    rendered = S.render_spec(parsed['vs'], parsed['routes'], parsed['hosts'])
    assert S.spec_hash(rendered) == S.spec_hash(spec)
    assert S.canonical(rendered) == S.canonical(spec)


def test_round_trip_without_default_route():
    spec = vs_spec(default=None)
    parsed = S.parse_spec(spec)
    assert parsed['vs']['df_forward_type'] is None and S.render_default(parsed['vs']) is None
    assert S.spec_hash(S.render_spec(parsed['vs'], parsed['routes'], parsed['hosts'])) == S.spec_hash(spec)


def test_render_orders_routes_by_priority_and_puts_default_last():
    vs = {'gateways': ['g'], 'df_forward_type': 'route', 'df_forward_detail': [{'destination': {'host': 'd'}}],
          'df_forward_timeout': None, 'df_extra': None, 'extra_spec': None}
    routes = [
        {'id': 2, 'priority': 20, 'name': None, 'match_rules': [{'uri': {'prefix': '/b'}}],
         'forward_type': 'route', 'forward_detail': [{'destination': {'host': 'b'}}]},
        {'id': 1, 'priority': 10, 'name': None, 'match_rules': [{'uri': {'prefix': '/a'}}],
         'forward_type': 'delegate', 'forward_detail': {'name': 'a', 'namespace': 'x'}},
    ]
    spec = S.render_spec(vs, routes, ['h'])
    assert [e.get('match') for e in spec['http']] == [[{'uri': {'prefix': '/a'}}], [{'uri': {'prefix': '/b'}}], None]
    assert spec['http'][0] == {'match': [{'uri': {'prefix': '/a'}}], 'delegate': {'name': 'a', 'namespace': 'x'}}
    assert spec['http'][-1] == {'route': [{'destination': {'host': 'd'}}]}
    manifest = S.manifest('istio-system', 'demo', spec)
    assert manifest['apiVersion'] == 'networking.istio.io/v1beta1' and manifest['metadata']['name'] == 'demo'


def test_hash_ignores_key_order_and_empty_values():
    a = {'hosts': ['x'], 'http': [{'match': [], 'route': [{'destination': {'host': 'd'}}], 'timeout': None}]}
    b = {'http': [{'route': [{'destination': {'host': 'd'}}]}], 'gateways': [], 'hosts': ['x']}
    assert S.spec_hash(a) == S.spec_hash(b)
    assert S.spec_hash({'hosts': ['x', 'y']}) != S.spec_hash({'hosts': ['y', 'x']})  # 顺序有意义


def test_shared_hash_ignores_hosts_only():
    assert S.shared_hash(vs_spec(hosts=['a'])) == S.shared_hash(vs_spec(hosts=['b', 'c']))
    assert S.shared_hash(vs_spec()) != S.shared_hash(vs_spec(default=None))


# ---------------------------------------------------------------- 名称规范
@pytest.mark.parametrize('name,rewrite,issue', [
    ('/a', {'uri': '/'}, None),
    (None, None, None),
    (None, {'uri': '/'}, '用了重写但没有名称'),
    ('/boss', None, '没用重写却设置了名称'),
])
def test_route_name_follows_rewrite(name, rewrite, issue):
    assert S.route_issue(name, rewrite) == issue


def test_lint_reports_position_and_label():
    http = [route('/a/', 1, name='/a', rewrite='/'), route('/boss/', 2, name='/boss'), route('/c/', 3, rewrite='/')]
    assert S.lint_http(http) == [
        '第 2 条规则(/boss)没用重写却设置了名称',
        '第 3 条规则(uri prefix /c/)用了重写但没有名称',
    ]


def test_describe_match():
    match = [{'uri': {'prefix': '/a/'}, 'headers': {'x-env': {'exact': 'gray'}}}, {'authority': {'exact': 'h'}}]
    assert S.describe_match(match) == 'uri prefix /a/ & header x-env exact gray | authority exact h'
    assert S.describe_match(None) == '无匹配条件'


# ---------------------------------------------------------------- 对比
def test_diff_aligns_rules_by_match_conditions():
    left = [route('/a/', 1), route('/b/', 2, name='/b', rewrite='/'), route('/c/', 3), route('/d/', 4)]
    right = [route('/a/', 1), route('/b/', 2, name='/b', rewrite='/', timeout='9s'), route('/x/', 9), route('/d/', 4)]
    rows = S.diff_http(left, right)
    ops = [(r['op'], r['label']) for r in rows]
    assert ops == [
        ('same', 'uri prefix /a/'),
        ('changed', '/b'),
        ('left_only', 'uri prefix /c/'),
        ('right_only', 'uri prefix /x/'),
        ('same', 'uri prefix /d/'),
    ]


def test_diff_marks_reordered_rules_as_moved():
    left = [route('/a/', 1), route('/b/', 2), route('/c/', 3)]
    right = [route('/c/', 3), route('/a/', 1), route('/b/', 2, timeout='1s')]
    rows = S.diff_http(left, right)
    assert len(rows) == 3
    moved = [r for r in rows if r['moved']]
    assert [r['label'] for r in moved] == ['uri prefix /c/'] and moved[0]['op'] == 'same'
    assert next(r for r in rows if r['label'] == 'uri prefix /b/')['op'] == 'changed'


def test_diff_specs_summary():
    left = vs_spec(hosts=['a', 'b'])
    right = vs_spec(hosts=['b', 'c'], routes=[route('/user-api/', 6001)], default=None)
    diff = S.diff_specs(left, right)
    assert not diff['same'] and not diff['shared_same']
    assert diff['hosts'] == {'same': False, 'left_only': ['a'], 'right_only': ['c']}
    assert diff['gateways']['same'] and not diff['default']['same'] and diff['default']['right'] is None
    assert diff['summary'] == {'left_only': 1, 'right_only': 0, 'changed': 0, 'domains': 0, 'moved': 0}
    same = S.diff_specs(left, vs_spec(hosts=['x']))
    assert same['shared_same'] and not same['same'] and same['summary']['left_only'] == 0


# ---------------------------------------------------------------- 状态 / 版本 / 组矩阵
def test_binding_status():
    rendered = vs_spec()
    changed = vs_spec(routes=[route('/a/', 1)])
    assert S.binding_status(rendered, None, None, collected=False) == S.UNKNOWN
    assert S.binding_status(rendered, None, None, collected=True) == S.MISSING
    assert S.binding_status(rendered, None, copy.deepcopy(rendered), True) == S.SYNCED
    # 集群里仍是上次下发的内容:组标准改了还没下发
    assert S.binding_status(rendered, S.spec_hash(changed), changed, True) == S.PENDING
    # 集群里不是上次下发的内容:被直接改过
    assert S.binding_status(rendered, S.spec_hash(rendered), changed, True) == S.DIFF
    assert S.binding_status(rendered, None, changed, True) == S.DIFF


def test_version_labels_rank_by_cluster_count():
    a, b = vs_spec(hosts=['1']), vs_spec(routes=[route('/a/', 1)])
    specs = {'beta-k': b, 'beta-p': a, 'prod-k': vs_spec(hosts=['2'])}
    labels, versions = S.version_labels(specs, ['beta-k', 'beta-p', 'prod-k', 'prod-p'])
    assert labels == {'beta-p': 'A', 'prod-k': 'A', 'beta-k': 'B'}
    assert [(v['label'], v['clusters']) for v in versions] == [('A', ['beta-p', 'prod-k']), ('B', ['beta-k'])]


def make_standard(spec, bindings, vs_id=1, namespace='istio-system', name='demo'):
    parsed = S.parse_spec(spec)
    return {'id': vs_id, 'namespace': namespace, 'name': name, **parsed['vs'], 'routes': parsed['routes'],
            'bindings': bindings, 'updated_at': None, 'updated_by': 'admin'}


CLUSTERS = [
    {'k8s_name': 'beta-k', 'env_type': 'test', 'collected_at': 'x'},
    {'k8s_name': 'beta-p', 'env_type': 'test', 'collected_at': 'x'},
    {'k8s_name': 'prod-a', 'env_type': 'prod', 'collected_at': 'x'},
    {'k8s_name': 'prod-b', 'env_type': 'prod', 'collected_at': None},
]


def test_matrix_unmanaged_rows_show_versions_and_coverage():
    snapshots = {
        ('beta-k', 'istio-system', 'only-test'): vs_spec(hosts=['t1']),
        ('beta-p', 'istio-system', 'only-test'): vs_spec(hosts=['t2'], routes=[route('/a/', 1)]),
        ('prod-a', 'istio-system', 'shared'): vs_spec(hosts=['p']),
        ('beta-k', 'istio-system', 'shared'): vs_spec(hosts=['t']),
    }
    rows = {r['name']: r for r in S.build_matrix(CLUSTERS, [], snapshots)}
    only_test = rows['only-test']
    assert only_test['vs_id'] is None and only_test['coverage'] == 'test_only'
    assert [v['label'] for v in only_test['versions']] == ['A', 'B']
    assert only_test['cells']['beta-k'] == {'in_cluster': True, 'version': 'A', 'bound': False,
                                           'hosts': ['t1'], 'status': S.UNMANAGED}
    assert only_test['cells']['prod-a']['status'] == S.NONE
    shared = rows['shared']
    assert shared['coverage'] == '' and len(shared['versions']) == 1 and shared['route_count'] == 2
    assert shared['has_default'] and shared['lint'] == []


def test_matrix_managed_row_statuses():
    standard_spec = vs_spec()
    pending_live = vs_spec(hosts=['p'], routes=[route('/a/', 1)])
    drifted_live = vs_spec(hosts=['k'], routes=[route('/z/', 9)])
    standard = make_standard(standard_spec, {
        'beta-k': {'hosts': ['a.example.com'], 'applied_hash': None, 'applied_at': None, 'applied_by': ''},
        'beta-p': {'hosts': ['p'], 'applied_hash': S.spec_hash(pending_live), 'applied_at': None, 'applied_by': ''},
        'prod-a': {'hosts': ['k'], 'applied_hash': S.spec_hash(vs_spec(hosts=['k'])), 'applied_at': None,
                   'applied_by': ''},
        'prod-b': {'hosts': ['q'], 'applied_hash': None, 'applied_at': None, 'applied_by': ''},
    })
    snapshots = {
        ('beta-k', 'istio-system', 'demo'): copy.deepcopy(standard_spec),
        ('beta-p', 'istio-system', 'demo'): pending_live,
        ('prod-a', 'istio-system', 'demo'): drifted_live,
    }
    row = S.build_matrix(CLUSTERS, [standard], snapshots)[0]
    assert row['vs_id'] == 1 and row['standard_version'] == 'A' and row['route_count'] == 2
    statuses = {k: c['status'] for k, c in row['cells'].items()}
    assert statuses == {'beta-k': S.SYNCED, 'beta-p': S.PENDING, 'prod-a': S.DIFF, 'prod-b': S.UNKNOWN}
    assert row['cells']['beta-p']['hosts'] == ['p'] and row['status_count'][S.SYNCED] == 1


def test_matrix_bound_and_unbound_cells():
    standard = make_standard(vs_spec(), {
        'beta-k': {'hosts': ['h'], 'applied_hash': None, 'applied_at': None, 'applied_by': ''},
    })
    snapshots = {('prod-a', 'istio-system', 'demo'): vs_spec(hosts=['x'])}
    row = S.build_matrix(CLUSTERS[:3], [standard], snapshots)[0]
    assert row['cells']['beta-k']['status'] == S.MISSING
    assert row['cells']['beta-p']['status'] == S.NONE
    assert row['cells']['prod-a']['status'] == S.UNBOUND and row['cells']['prod-a']['hosts'] == ['x']
    assert row['coverage'] == ''


# ---------------------------------------------------------------- 域名条件按集群
def host_rule(site=None, shared='svc.intra.example.com', service='svc', **extra):
    """按域名转发的规则:本集群专属域名(svc-<site>)+ 共用域名,各集群只差域名。"""
    match = []
    if site:
        match.append({'authority': {'exact': f'svc-{site}.intra.example.com'}})
    match.append({'authority': {'exact': shared}})
    entry = {'match': match, 'route': [{'destination': {'host': f'{service}.ns.svc.cluster.local'}}]}
    entry.update(extra)
    return entry


def site_spec(site, routes=None):
    hosts = [f'svc-{site}.intra.example.com', 'svc.intra.example.com', f'other-{site}.intra.example.com']
    http = routes if routes is not None else [host_rule(site), route('/api/', 1)]
    return {'hosts': hosts, 'gateways': ['istio-system/internal'], 'http': http}


# 线上测试集群里见过的几种写法:authority 和 headers.host 混用、多个域名都带同一个 uri、两组条件
MIXED = [{'headers': {'host': {'exact': 'svc-beta-p.example.com'}}}, {'authority': {'exact': 'svc-beta.example.com'}}]
SAME_URI = [{'headers': {'host': {'exact': f'f-{s}.example.com'}}, 'uri': {'prefix': '/passport'}} for s in 'abc'] + [
    {'authority': {'exact': f'f-{s}.example.com'}, 'uri': {'prefix': '/passport'}} for s in 'de']
TWO_BLOCKS = [{'headers': {'host': {'exact': 'passport.example.com'}}},
              {'headers': {'host': {'exact': 'api.example.com'}}, 'uri': {'prefix': '/passport'}},
              {'authority': {'exact': 'api-k.example.com'}, 'uri': {'prefix': '/passport'}}]


@pytest.mark.parametrize('match,blocks,sizes', [
    (MIXED, [{S.DOMAIN_KEY: True}], [2]),
    (SAME_URI, [{'uri': {'prefix': '/passport'}, S.DOMAIN_KEY: True}], [5]),
    (TWO_BLOCKS, [{S.DOMAIN_KEY: True}, {'uri': {'prefix': '/passport'}, S.DOMAIN_KEY: True}], [1, 2]),
])
def test_split_and_render_domain_conditions_round_trip(match, blocks, sizes):
    template, lists = S.split_match(match)
    assert template == blocks and [len(items) for items in lists] == sizes
    assert S.canonical(S.render_match(template, lists)) == S.canonical(match)
    assert lists[0][0]['field'] == 'host' and lists[0][0]['op'] == 'exact'


def test_domain_conditions_that_stay_literal():
    both = {'authority': {'exact': 'a'}, 'headers': {'host': {'exact': 'b'}}}
    two_ops = {'authority': {'exact': 'a', 'prefix': 'b'}}
    uri_only = {'uri': {'prefix': '/x'}}
    template, lists = S.split_match([uri_only, both, two_ops])
    assert template == [uri_only, both, two_ops] and lists == []
    # headers 里除了 host 还有别的 header:host 拆出去,其他 header 留在模板里
    template, lists = S.split_match([{'headers': {'host': {'exact': 'a'}, 'x-env': {'exact': 'gray'}}}])
    assert template == [{'headers': {'x-env': {'exact': 'gray'}}, S.DOMAIN_KEY: True}]
    assert S.render_match(template, lists) == [{'headers': {'x-env': {'exact': 'gray'}, 'host': {'exact': 'a'}}}]


def test_parse_keeps_template_and_cluster_domains():
    parsed = S.parse_spec(site_spec('p'))
    domain_route, plain_route = parsed['routes']
    assert domain_route['match_rules'] == [{S.DOMAIN_KEY: True}]
    assert [i['value'] for i in domain_route['domain_lists'][0]] == ['svc-p.intra.example.com', 'svc.intra.example.com']
    assert plain_route['match_rules'] == [{'uri': {'prefix': '/api/'}}] and plain_route['domain_lists'] == []


def make_domain_standard(clusters=('p', 'k'), bound=None):
    """以 p 的现状生成组标准,各集群的域名取各自的现状。"""
    parsed = S.parse_spec(site_spec('p'))
    routes = parsed['routes']
    for index, r in enumerate(routes):
        r['id'], r['domains'] = index + 1, {}
        for site in clusters:
            lists = S.parse_spec(site_spec(site))['routes'][index]['domain_lists']
            if lists:
                r['domains'][site] = lists
    bindings = {site: {'hosts': site_spec(site)['hosts'], 'applied_hash': None} for site in (bound or clusters)}
    return {'id': 1, 'namespace': 'istio-system', 'name': 'demo', **parsed['vs'], 'routes': routes,
            'bindings': bindings, 'updated_at': None, 'updated_by': ''}


def test_render_uses_each_clusters_own_domains():
    standard = make_domain_standard()
    for site in ('p', 'k'):
        rendered = S.standard_spec(standard, site_spec(site)['hosts'], site)
        assert S.spec_hash(rendered) == S.spec_hash(site_spec(site))
    shared = S.standard_spec(standard)
    assert shared['http'][0]['match'] == [{'authority': {'exact': S.DOMAIN_PLACEHOLDER}}]
    assert S.describe_match(shared['http'][0]['match']) == '域名按集群'
    assert S.describe_match(standard['routes'][0]['match_rules']) == '域名按集群'


def test_rule_digest_and_versions_ignore_domain_values():
    p, k = site_spec('p'), site_spec('k')
    assert S.spec_hash(p) != S.spec_hash(k) and S.shared_hash(p) != S.shared_hash(k)
    assert S.rule_digest(p) == S.rule_digest(k)
    assert S.rule_digest(p) == S.rule_digest(S.standard_spec(make_domain_standard()))
    # 规则真的不一样(转发目标不同)还是不同版本
    other = site_spec('k', routes=[host_rule('k', service='other'), route('/api/', 1)])
    labels, versions = S.version_labels({'p': p, 'k': k, 'x': other}, ['p', 'k', 'x'])
    assert labels == {'p': 'A', 'k': 'A', 'x': 'B'}


def test_missing_domains_make_cluster_incomplete():
    standard = make_domain_standard(clusters=('p',), bound=('p', 'k'))
    assert S.missing_domains(standard, 'p') == []
    assert S.missing_domains(standard, 'k') == ['第 1 条规则(域名按集群 → svc.ns)']
    status, _ = S.cluster_status(standard, standard['bindings']['k'], 'k', site_spec('k'), True)
    assert status == S.INCOMPLETE
    status, _ = S.cluster_status(standard, standard['bindings']['p'], 'p', site_spec('p'), True)
    assert status == S.SYNCED
    # 设置了但一个域名都没有:规则会变成匹配所有请求,同样不能下发
    standard['routes'][0]['domains']['k'] = [[]]
    assert S.missing_domains(standard, 'k') == ['第 1 条规则(域名按集群 → svc.ns)']


def test_diff_marks_rules_that_only_differ_in_domains():
    diff = S.diff_specs(site_spec('p'), site_spec('k'))
    assert not diff['shared_same'] and diff['rules_same']
    assert [r['op'] for r in diff['routes']] == ['domains', 'same']
    assert diff['summary']['domains'] == 1 and diff['summary']['changed'] == 0


def test_align_rules_with_identical_templates():
    """一个 VS 里多条「按域名转发到不同服务」的规则去掉域名后一模一样,新增一条不能把后面的都对错位。"""
    def rule(name, suffix=''):
        return {'match': [{'headers': {'host': {'exact': f'{name}{suffix}.example.com'}}}],
                'route': [{'destination': {'host': name}}]}

    left = [rule('a'), rule('b'), rule('c')]
    right = [rule('a'), rule('new'), rule('b'), rule('c')]
    assert [r['op'] for r in S.diff_http(left, right)] == ['same', 'right_only', 'same', 'same']
    # 两个集群各自的域名:按位置一一对上,标「只是域名不同」
    kunlun = [rule(name, '-k') for name in 'abc']
    assert [(r['op'], r['left_index'], r['right_index']) for r in S.diff_http(left, kunlun)] == [
        ('domains', 0, 0), ('domains', 1, 1), ('domains', 2, 2)]


def test_aligned_domains_takes_values_from_matching_rules():
    base = site_spec('p')['http']
    other = site_spec('k', routes=[route('/new/', 2), host_rule('k'), route('/api/', 1)])
    found = S.aligned_domains(base, other)
    assert list(found) == [0]
    assert [i['value'] for i in found[0][0]] == ['svc-k.intra.example.com', 'svc.intra.example.com']
    # 结构不同(多了一组条件)对不上,不取
    extra_block = {**host_rule('k'), 'match': host_rule('k')['match'] + [{'uri': {'exact': '/x'}}]}
    assert S.aligned_domains(base, site_spec('k', routes=[extra_block])) == {}
    assert S.aligned_domains(base, None) == {}


def test_merge_adjacent_domain_blocks():
    template = [{S.DOMAIN_KEY: True}, {S.DOMAIN_KEY: True}, {'uri': {'prefix': '/x'}}, {S.DOMAIN_KEY: True}]
    item = {'field': 'authority', 'op': 'exact'}
    lists = {'p': [[{**item, 'value': 'a'}], [{**item, 'value': 'b'}], [{**item, 'value': 'c'}]]}
    merged, domains = S.merge_domain_blocks(template, lists)
    assert merged == [{S.DOMAIN_KEY: True}, {'uri': {'prefix': '/x'}}, {S.DOMAIN_KEY: True}]
    assert [[i['value'] for i in block] for block in domains['p']] == [['a', 'b'], ['c']]
    assert S.split_match(S.render_match(merged, domains['p']))[0] == merged


def test_lint_domains_requires_values_in_hosts():
    spec = {'hosts': ['a.example.com', '*.wild.example.com'],
            'http': [{'match': [{'authority': {'exact': 'a.example.com:8080'}},
                                {'headers': {'host': {'exact': 'x.wild.example.com'}}},
                                {'authority': {'exact': 'gone.example.com'}},
                                {'authority': {'prefix': 'anything'}}],
                      'route': [{'destination': {'host': 'a'}}]}]}
    issues = S.lint_domains(spec, 'beta-k:')
    assert len(issues) == 1 and issues[0].startswith('beta-k:第 1 条规则(')
    assert '域名 gone.example.com 不在 hosts 里' in issues[0]
    assert S.lint_domains(site_spec('p')) == []


def test_matrix_domain_only_difference_is_one_version():
    clusters = [{'k8s_name': 'p', 'env_type': 'prod', 'collected_at': 'x'},
                {'k8s_name': 'k', 'env_type': 'prod', 'collected_at': 'x'}]
    snapshots = {('p', 'istio-system', 'demo'): site_spec('p'), ('k', 'istio-system', 'demo'): site_spec('k')}
    row = S.build_matrix(clusters, [], snapshots)[0]
    assert len(row['versions']) == 1 and row['lint'] == []

    standard = make_domain_standard()
    row = S.build_matrix(clusters, [standard], snapshots)[0]
    assert {k: c['status'] for k, c in row['cells'].items()} == {'p': S.SYNCED, 'k': S.SYNCED}
    assert row['standard_version'] == 'A'
    standard['routes'][0]['domains'].pop('k')
    row = S.build_matrix(clusters, [standard], snapshots)[0]
    assert row['cells']['k']['status'] == S.INCOMPLETE
