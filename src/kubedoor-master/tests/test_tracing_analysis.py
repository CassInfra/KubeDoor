"""链路追踪：链路分析（tracing/analysis.py）——概览、根因、关键路径、N+1、折叠、按 max_nodes 裁剪、focus、输出大小。"""
import json

import pytest

from tracing import analysis, fields, model
from tracing_fakes import TraceBuilder

TRACE = '0af7651916cd43dd8448eb211c80319c'
ROOT = 'ee'


def sid(value):
    """测试里的 Span ID 都是短的十六进制，归一化后左边补 0"""
    return value.rjust(16, '0')


def build(builder, cluster_key='k8sname', aliases=None):
    [trace] = model.parse_document(builder.doc())
    model.derive(trace.spans, fields.default_fields(), cluster_key, fields.cluster_lookup(aliases or {'prod': []}))
    return trace.spans


def analyze(builder, **kwargs):
    return analysis.analyze(build(builder), TRACE, **kwargs)


def ids(tree):
    return [node['id'] for node in tree if 'id' in node]


# ---------------------------------------------------------------------------
# outcome、概览
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('root, child, outcome', [
    ({'error': True}, {}, 'failed'),
    ({'http': 502}, {}, 'failed'),  # 根是服务端、状态码 ≥ 500
    ({'http': 404}, {}, 'client_error'),
    ({'http': 200}, {'error': True}, 'recovered'),
    ({'http': 200}, {}, 'succeeded'),
])
def test_outcome(root, child, outcome):
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=10, **root)
    builder.span('c1', 'call', parent=ROOT, kind='client', start=1, duration=2, **child)
    result = analyze(builder)
    assert result['trace']['outcome'] == outcome
    assert (analysis.NOTE_RECOVERED in result['analysis']['notes']) == (outcome == 'recovered')


def test_outcome_unknown_without_root():
    builder = TraceBuilder().span('a1', 'x', parent='0dead').span('b1', 'y', parent='a1', start=1)
    result = analyze(builder)
    assert result['trace']['outcome'] == 'unknown' and result['trace']['root'] is None
    assert result['trace']['orphan_spans'] == 1 and result['trace']['roots_count'] == 0
    notes = ' '.join(result['analysis']['notes'])
    assert '父 Span 不在链路里' in notes and '没有根 Span' in notes
    assert ids(result['tree']) == [sid('a1'), sid('b1')]


def test_overview_fields():
    builder = TraceBuilder()
    builder.span(ROOT, 'GET /x', duration=100, http=200, service='gateway')
    builder.span('c1', 'POST', parent=ROOT, kind='client', start=5, duration=50, service='gateway')
    builder.span('c2', 'POST /y', parent='c1', start=6, duration=40, service='svc-b', cluster='kunlun', error=True)
    spans = build(builder, aliases={'prod': [], 'kunlun-env': ['kunlun']})
    result = analysis.analyze(spans, TRACE, extra={'url': 'http://j/trace/x', 'time_range_used': '最近 1 小时'})
    trace = result['trace']
    assert trace['trace_id'] == TRACE and trace['span_count'] == 3 and trace['service_count'] == 2
    assert trace['duration_ms'] == 100.0 and trace['start'].endswith('+08:00')
    assert trace['services'] == [{'name': 'gateway', 'span_count': 2, 'error_span_count': 0},
                                 {'name': 'svc-b', 'span_count': 1, 'error_span_count': 1}]
    assert trace['clusters'] == [{'value': 'prod', 'env': 'prod', 'span_count': 2},
                                 {'value': 'kunlun', 'env': 'kunlun-env', 'span_count': 1}]
    assert trace['root'] == {'span_id': sid(ROOT), 'service': 'gateway', 'name': 'GET /x', 'kind': 'server',
                             'status': 'unset', 'http_status': 200, 'duration_ms': 100.0}
    assert trace['url'] == 'http://j/trace/x' and trace['time_range_used'] == '最近 1 小时'
    assert trace['roots_count'] == 1 and trace['orphan_spans'] == 0
    # 和根不同集群的节点才写 cluster；涉及多个集群写进 notes
    other = next(node for node in result['tree'] if node['name'] == 'POST /y')
    assert other['cluster'] == {'value': 'kunlun', 'env': 'kunlun-env'}
    assert all('cluster' not in node for node in result['tree'] if node['name'] != 'POST /y')
    assert any('链路涉及多个集群' in note for note in result['analysis']['notes'])


# ---------------------------------------------------------------------------
# 根因
# ---------------------------------------------------------------------------
def test_root_cause_walks_into_waited_child():
    """网关 504：它在等下游（下游比网关的超时还长），根因在下游"""
    builder = TraceBuilder()
    builder.span('a1', 'POST bizme', service='gateway', duration=7000, http=504, error=True)
    builder.span('a2', 'POST', parent='a1', service='gateway', kind='client', start=1, duration=6995)
    builder.span('a3', 'POST /retry', parent='a2', service='inventory', start=2, duration=11000, http=200)
    for i in range(3):
        builder.span(f'b{i}', f'SELECT t{i}', parent='a3', service='inventory', kind='client', start=10 + i * 100,
                     duration=50)
    [cause] = analyze(builder)['analysis']['root_cause']
    assert cause['id'] == sid('a3') and cause['service'] == 'inventory' and cause['name'] == 'POST /retry'
    assert cause['reason'].startswith(analysis.REASON_WAIT) and '比报错的调用（7000.0 ms）还长' in cause['reason']
    assert cause['evidence']['duration_ms'] == 11000.0 and cause['evidence']['http_status'] == 200
    assert cause['evidence']['error_span'] == {'id': sid('a1'), 'service': 'gateway', 'name': 'POST bizme',
                                               'duration_ms': 7000.0, 'http_status': 504}
    assert cause['cluster'] == {'value': 'prod', 'env': 'prod'} and cause['k8s_pod'] == 'inventory-pod-1'


def test_root_cause_wait_stops_at_client_without_server_span():
    builder = TraceBuilder().span('a1', 'GET /x', duration=3000, http=504, error=True)
    builder.span('a2', 'GET', parent='a1', kind='client', start=1, duration=2990, attributes={'server.address': 'legacy'})
    [cause] = analyze(builder)['analysis']['root_cause']
    assert cause['id'] == sid('a2') and '它是对 legacy 的调用，对方没有记录 Span' in cause['reason']


def test_root_cause_wait_needs_100ms_and_90_percent():
    builder = TraceBuilder()
    builder.span('a1', 'GET /fast', duration=80, error=True)  # 不到 100ms：不往下走
    builder.span('a2', 'call', parent='a1', kind='client', start=1, duration=79)
    builder.span('b1', 'GET /slow', start=100, duration=1000, error=True)
    builder.span('b2', 'call', parent='b1', kind='client', start=101, duration=800)  # 不到 90%
    causes = analyze(builder)['analysis']['root_cause']
    assert [cause['id'] for cause in causes] == [sid('a1'), sid('b1')]
    assert all(cause['reason'] == analysis.REASON_DEEPEST.format(service='svc-a') for cause in causes)


def test_callee_unreachable():
    builder = TraceBuilder()
    builder.span(ROOT, 'GET /order', duration=50, http=500, error=True)
    builder.span('c1', 'POST', parent=ROOT, kind='client', start=1, duration=30, error=True,
                 attributes={'server.address': 'stock-service', 'server.port': 8080,
                             'error.type': 'java.net.ConnectException'})
    [cause] = analyze(builder)['analysis']['root_cause']
    assert cause['id'] == sid('c1') and cause['callee_unreachable'] is True and cause['peer'] == 'stock-service:8080'
    assert cause['reason'].startswith('调用 stock-service:8080 失败且对方没有记录：被调方可能不可用')
    assert cause['evidence']['error'] == 'java.net.ConnectException'


@pytest.mark.parametrize('attributes, http, unreachable, reason', [
    ({'server.address': 'lb'}, 503, True, '返回 HTTP 503，对方没有记录 Span：被调方可能不可用'),
    ({'server.address': 'lb'}, 404, False, '返回 HTTP 404，对方没有记录 Span：可能对方没接入链路'),
    ({'db.system': 'mysql', 'server.address': 'db'}, None, False, '数据库 / 缓存调用出错（mysql）'),
])
def test_client_error_without_server_span(attributes, http, unreachable, reason):
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=50, error=True)
    builder.span('c1', 'call', parent=ROOT, kind='client', start=1, duration=30, error=True, http=http,
                 attributes=attributes)
    [cause] = analyze(builder)['analysis']['root_cause']
    assert reason in cause['reason'] and cause.get('callee_unreachable', False) is unreachable


def test_timeout_without_spans_below():
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=3100, error=True)
    builder.span('c1', 'GET', parent=ROOT, kind='client', start=1, duration=40, error=True,
                 events=[('exception', {'exception.type': 'java.net.SocketTimeoutException',
                                        'exception.message': 'Read timed out'})])
    [cause] = analyze(builder)['analysis']['root_cause']
    assert cause['reason'] == analysis.REASON_TIMEOUT
    assert cause['evidence']['exception_type'] == 'java.net.SocketTimeoutException'
    assert cause['evidence']['exception_message'] == 'Read timed out'
    assert cause['evidence']['error'] == 'java.net.SocketTimeoutException: Read timed out'


def test_root_cause_by_status_only_and_limit_of_three():
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=50, http=500)  # 没有出错 Span，只靠状态码
    [cause] = analyze(builder)['analysis']['root_cause']
    assert cause['id'] == sid(ROOT) and '链路里没有出错的 Span' in cause['reason']
    # 多个独立的出错点：只列 3 个，先出错的在前
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=100)
    for i in range(5):
        builder.span(f'e{i}', f'call{i}', parent=ROOT, kind='internal', start=10 * i, duration=5, error=True)
    result = analyze(builder)
    assert [cause['id'] for cause in result['analysis']['root_cause']] == [sid('e0'), sid('e1'), sid('e2')]
    assert any('共有 5 个独立的出错点' in note for note in result['analysis']['notes'])
    assert result['analysis']['errors']['total'] == 5 and len(result['analysis']['errors']['spans']) == 5
    assert result['trace']['outcome'] == 'recovered'


def test_errors_deepest_first_with_limit():
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=1000, error=True)
    parent = ROOT
    for i in range(25):
        builder.span(f'd{i:02d}', f'call{i}', parent=parent, start=i, duration=900 - i, error=True)
        parent = f'd{i:02d}'
    errors = analyze(builder)['analysis']['errors']
    assert errors['total'] == 26 and len(errors['spans']) == 20
    assert [item['depth'] for item in errors['spans'][:3]] == [25, 24, 23]
    assert set(errors['spans'][0]) == {'id', 'service', 'name', 'kind', 'k8s_pod', 'cluster', 'depth'}


# ---------------------------------------------------------------------------
# 关键路径
# ---------------------------------------------------------------------------
def test_critical_path_like_jaeger_ui():
    builder = TraceBuilder().span(ROOT, 'root', duration=100)
    builder.span('a1', 'A', parent=ROOT, start=10, duration=30)  # 10..40
    builder.span('b1', 'B', parent=ROOT, start=50, duration=40)  # 50..90
    builder.span('c1', 'C', parent='b1', start=60, duration=35)  # 60..95，超出 B 的部分裁掉
    builder.span('d1', 'D', parent=ROOT, start=120, duration=5)  # 完全在根之外：连同子树丢掉
    builder.span('e1', 'E', parent='a1', start=5, duration=10)  # 开始早于 A：裁到 10..15
    index = analysis.Index(build(builder))
    sections, total = analysis.critical_path(index)
    as_ms = {index.by_id[key].name: value / 1e6 for key, value in sections.items()}
    assert total == 100_000_000 and sum(sections.values()) == total
    assert as_ms == {'root': 30.0, 'B': 10.0, 'C': 30.0, 'A': 25.0, 'E': 5.0}
    summary = analysis.critical_summary(index, sections, total)
    assert summary['total_ms'] == 100.0
    assert [(item['name'], item['self_ms'], item['pct']) for item in summary['top']] == [
        ('root', 30.0, 30.0), ('C', 30.0, 30.0), ('A', 25.0, 25.0), ('B', 10.0, 10.0), ('E', 5.0, 5.0)]


def test_critical_path_serial_loop_and_self_time():
    builder = TraceBuilder().span(ROOT, 'root', duration=100)
    for i in range(10):
        builder.span(f'c{i}', 'SELECT t', parent=ROOT, kind='client', start=i * 10 + 1, duration=8)
    builder.span('f0', 'zero', parent=ROOT, start=0, duration=0)  # 零耗时
    index = analysis.Index(build(builder))
    sections, total = analysis.critical_path(index)
    assert sum(sections.values()) == total == 100_000_000
    assert sections[sid(ROOT)] == 20_000_000  # 10 段 2ms 的空隙
    assert index.self_ns[sid(ROOT)] == 20_000_000
    # 自身耗时：子 Span 重叠的部分只算一次
    builder = TraceBuilder().span(ROOT, 'root', duration=100)
    builder.span('a1', 'A', parent=ROOT, start=10, duration=50).span('b1', 'B', parent=ROOT, start=30, duration=50)
    index = analysis.Index(build(builder))
    assert index.self_ns[sid(ROOT)] == 30_000_000


# ---------------------------------------------------------------------------
# N+1
# ---------------------------------------------------------------------------
def test_n_plus_one_serial_vs_parallel():
    builder = TraceBuilder().span(ROOT, 'GET /list', duration=1000)
    for i in range(12):  # 串行：一个接一个
        builder.span(f'a{i:02d}', 'SELECT item', parent=ROOT, kind='client', start=i * 50, duration=40)
    for i in range(15):  # 并行：同时发出（扇出，不是 N+1）
        builder.span(f'b{i:02d}', 'GET /price', parent=ROOT, kind='client', service='svc-b', start=700, duration=200)
    for i in range(9):  # 不到 10 个：不算
        builder.span(f'c{i}', 'GET /few', parent=ROOT, kind='client', start=950 + i, duration=1)
    groups = analyze(builder)['analysis']['n_plus_one']
    assert [(g['child']['name'], g['count'], g['serial']) for g in groups] == [
        ('SELECT item', 12, True), ('GET /price', 15, False)]
    serial = groups[0]
    assert serial['parent'] == {'id': sid(ROOT), 'service': 'svc-a', 'name': 'GET /list'}
    assert serial['child'] == {'service': 'svc-a', 'name': 'SELECT item'}
    assert serial['window_ms'] == 590.0 and serial['sum_ms'] == 480.0
    assert serial['example_ids'] == [sid('a00'), sid('a01'), sid('a02')]
    assert groups[1]['window_ms'] == 200.0 and groups[1]['sum_ms'] == 3000.0


# ---------------------------------------------------------------------------
# 调用树：节点、折叠、裁剪、focus
# ---------------------------------------------------------------------------
def test_tree_node_fields():
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=100, http=200)
    builder.span('c1', 'POST', parent=ROOT, kind='client', start=10, duration=50, error=True, http=503,
                 status_message='upstream ' + 'x' * 300, attributes={'server.address': 'svc-b'})
    tree = analyze(builder)['tree']
    assert tree[0] == {'id': sid(ROOT), 'depth': 0, 'service': 'svc-a', 'name': 'GET /x', 'kind': 'server',
                       'start_ms': 0.0, 'duration_ms': 100.0, 'self_ms': 50.0, 'http_status': 200,
                       'k8s_pod': 'svc-a-pod-1'}
    child = tree[1]
    assert child['status'] == 'error' and child['http_status'] == 503 and child['peer'] == 'svc-b'
    assert child['start_ms'] == 10.0 and len(child['error']) == 200 and 'cluster' not in child


def test_folding_keeps_first_slowest_and_errors():
    # 并行的兄弟：只有最慢的那个在关键路径上（串行的兄弟都在关键路径上，按约定不折叠）
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=1000)
    for i, duration in enumerate([5, 9, 50, 7, 6, 8]):
        builder.span(f'a{i}', 'SELECT t', parent=ROOT, kind='client', start=10 + i * 0.1, duration=duration,
                     error=(i == 4))
    for i in range(3):  # 少于 4 个：不折叠
        builder.span(f'b{i}', 'SELECT b', parent=ROOT, kind='client', start=200 + i, duration=1)
    for i in range(4):  # 保留第 1 个、最慢的、出错的之后只剩 1 个：不折叠
        builder.span(f'c{i}', 'SELECT c', parent=ROOT, kind='client', start=300 + i, duration=1 + i, error=(i == 1))
    tree = analyze(builder)['tree']
    shown = set(ids(tree))
    # 第 1 个（a0）、最慢的（a2）、出错的（a4）保留，其余 3 个合并
    assert {sid('a0'), sid('a2'), sid('a4')} <= shown and not {sid('a1'), sid('a3'), sid('a5')} & shown
    [fold] = [node for node in tree if 'folded' in node]
    assert fold == {'depth': 1, 'folded': {'count': 3, 'service': 'svc-a', 'name': 'SELECT t', 'kind': 'client',
                                           'total_ms': 24.0, 'min_ms': 7.0, 'max_ms': 9.0, 'errors': 0}}
    assert {sid(f'b{i}') for i in range(3)} <= shown and {sid(f'c{i}') for i in range(4)} <= shown
    # 折叠项排在第一个被折叠的兄弟（a1）原来的位置
    position = tree.index(fold)
    assert tree[position - 1]['id'] == sid('a0') and tree[position + 1]['id'] == sid('a2')


def test_folded_subtrees_are_counted():
    builder = TraceBuilder().span(ROOT, 'GET /x', duration=1000)
    for i in range(5):  # 并行调用
        builder.span(f'a{i}', 'POST', parent=ROOT, kind='client', start=i * 0.1, duration=50 + i)
        builder.span(f'b{i}', 'POST /y', parent=f'a{i}', service='svc-b', start=i * 0.1 + 1, duration=40)
    tree = analyze(builder)['tree']
    [fold] = [node['folded'] for node in tree if 'folded' in node]
    assert fold['count'] == 3 and fold['descendants'] == 3  # a1~a3 连同各自的子 Span 一起折叠


def test_max_nodes_prunes_deepest_unprotected_first():
    builder = TraceBuilder().span(ROOT, 'root', duration=10000)
    for i in range(30):
        builder.span(f'a{i:02d}', f'A{i}', parent=ROOT, start=i * 300, duration=200)
        for j in range(3):
            builder.span(f'b{i:02d}{j}', f'B{i}-{j}', parent=f'a{i:02d}', start=i * 300 + j * 50 + 1, duration=40,
                         error=(i == 29 and j == 2))
    result = analyze(builder, max_nodes=50)
    tree = result['tree']
    assert len(tree) <= 50 and result['tree_stats']['max_nodes'] == 50 and result['truncated'] is False
    shown = set(ids(tree))
    assert {sid(ROOT), sid('a29'), sid('b292')} <= shown  # 出错的 Span 和它的祖先保留
    hidden = sum(node.get('hidden_children', 0) for node in tree)
    assert hidden + len(shown) == 121  # 每个 Span 要么显示，要么记在某个父节点的 hidden_children 里
    assert all(sid(f'a{i:02d}') in shown for i in range(30))  # 先剪最深一层（B），A 层都还在


def test_focus_span_path_and_three_levels():
    builder = TraceBuilder().span(ROOT, 'root', duration=1000)
    builder.span('ff', 'sibling', parent=ROOT, start=1, duration=10)
    parent = ROOT
    for depth in range(1, 8):
        builder.span(f'd{depth}', f'level{depth}', parent=parent, start=depth * 10, duration=500 - depth)
        parent = f'd{depth}'
    result = analyze(builder, focus_span=sid('d2'))
    tree = result['tree']
    assert ids(tree) == [sid(ROOT), sid('d1'), sid('d2'), sid('d3'), sid('d4'), sid('d5')]
    assert tree[0]['hidden_children'] == 1  # 根下面的 sibling 不在路径上
    assert tree[-1]['hidden_children'] == 2  # d5 下面还有 d6、d7
    missing = analyze(builder, focus_span='abcd')
    assert any('focus_span abcd 不在这条链路里' in note for note in missing['analysis']['notes'])
    assert len(ids(missing['tree'])) == 9


def test_output_size_shrinks_max_nodes_then_drops_details():
    builder = TraceBuilder().span(ROOT, 'root', duration=100000)
    for i in range(300):
        builder.span(f'1{i:04x}', f'call-{i}-' + 'x' * 150, parent=ROOT, kind='client', start=i * 300, duration=100,
                     attributes={'server.address': 'peer-' + 'y' * 100})
    result = analyze(builder, max_nodes=400)
    size = len(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    assert result['truncated'] is True and size <= analysis.MAX_OUTPUT_CHARS
    assert result['tree_stats']['max_nodes'] in (120, 80, 50) and result['tree_stats']['total_spans'] == 301
    # 降到 50 个还超：去掉 peer / error
    builder = TraceBuilder().span(ROOT, 'root', duration=100000)
    for i in range(60):
        builder.span(f'1{i:04x}', 'n' * 900, parent=ROOT, kind='client', start=i * 300, duration=100,
                     attributes={'server.address': 'p' * 900}, error=True, status_message='e' * 300)
    lean = analyze(builder, max_nodes=50)
    assert lean['truncated'] is True and all('peer' not in node and 'error' not in node for node in lean['tree'])


def test_index_handles_duplicates_and_cycles():
    builder = TraceBuilder().span(ROOT, 'root', duration=100)
    builder.span(ROOT, 'dup', duration=5)
    builder.span('a1', 'X', parent='b1', start=1, duration=1).span('b1', 'Y', parent='a1', start=2, duration=1)
    result = analyze(builder)
    notes = ' '.join(result['analysis']['notes'])
    assert '重复的 Span ID' in notes and '成环' in notes
    assert result['trace']['span_count'] == 3 and len(ids(result['tree'])) == 3


def test_warnings_note_and_slowest():
    builder = TraceBuilder().span(ROOT, 'root', duration=100, attributes={'@jaeger@warnings': ['clock skew adjusted']})
    builder.span('a1', 'A', parent=ROOT, start=10, duration=60)
    result = analyze(builder)
    assert any('时钟偏差' in note and 'clock skew adjusted' in note for note in result['analysis']['notes'])
    assert [(item['name'], item['self_ms']) for item in result['analysis']['slowest']] == [('A', 60.0), ('root', 40.0)]
