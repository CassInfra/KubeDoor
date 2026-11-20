"""链路分析（约定 §6.2~§6.5）：概览、调用树、出错、根因、关键路径、N+1、最慢。

纯函数，入参是 model.derive 处理过的 Span 列表（同一条链路）。CPU 开销和 Span 数成正比，
调用方放在线程里执行（asyncio.to_thread），不占 event loop。

关键路径的算法照搬 Jaeger UI（Apache-2.0）：jaeger-ui packages/jaeger-ui/src/components/TracePage/
CriticalPath（jaeger 主仓库 cmd/jaeger/internal/extension/jaegermcp/internal/criticalpath 也是同一个算法）：
先把子 Span 裁剪到父 Span 的时间范围内（完全在外面的连同子树丢掉）；从根开始，在当前时间点之前结束的子 Span
里取最后结束的（LFC）进去，当前时间点移到它的开始，重复；子 Span 之间的空隙算父 Span 自身的时间。
"""

import json
from collections import defaultdict

from .common import iso_ns
from .model import exception_info

ROOT_CAUSE_LIMIT = 3
ERROR_LIMIT = 20
CRITICAL_TOP = 8
N_PLUS_ONE_MIN = 10
N_PLUS_ONE_LIMIT = 5
SLOWEST_LIMIT = 5
FOLD_MIN = 4  # 同一父节点下 (service, name, kind) 相同的兄弟有这么多个才折叠
WAIT_RATIO = 0.9  # 子 Span 耗时 ≥ 父的 90% 算「在等」
WAIT_MIN_NS = 100 * 1_000_000  # 父 Span 至少 100ms 才看「在等」
FOCUS_DEPTH = 3
MAX_OUTPUT_CHARS = 60000
SHRINK_STEPS = (120, 80, 50)
TREE_ERROR_CHARS = 200
EXCEPTION_CHARS = 300
TIMEOUT_WORDS = ('timeout', 'timed out', '超时')

REASON_WAIT = '父调用在等子调用（超时 / 耗尽时间），原因在子调用'
REASON_TIMEOUT = '调用超时，被调方没有记录 Span（可能没接入链路，或请求没处理完）'
REASON_DEEPEST = '这是最深的出错点（它下面的调用都没有出错），错误发生在 {service} 自身'
NOTE_RECOVERED = '请求最终成功，中间的错误已恢复（重试或降级），不是请求失败的原因'


def _ms(nanos):
    return round(nanos / 1e6, 1)


def _is_timeout(text):
    lower = (text or '').lower()
    return any(word in lower for word in TIMEOUT_WORDS)


# ---------------------------------------------------------------------------
# 索引：父子关系、深度、子树大小、自身耗时
# ---------------------------------------------------------------------------
class Index:
    def __init__(self, spans):
        by_id, duplicates = {}, 0
        for span in spans:
            if span.span_id in by_id:
                duplicates += 1
                continue
            by_id[span.span_id] = span
        self.by_id, self.duplicates = by_id, duplicates
        self.spans = list(by_id.values())
        children, roots, orphans = defaultdict(list), [], []
        for span in self.spans:
            if not span.parent_id:
                roots.append(span)
            elif span.parent_id in by_id and span.parent_id != span.span_id:
                children[span.parent_id].append(span)
            else:
                orphans.append(span)
        order_key = lambda span: (span.start_ns, -span.end_ns, span.span_id)  # noqa: E731
        for kids in children.values():
            kids.sort(key=order_key)
        self.children = children
        self.roots = sorted(roots, key=order_key)
        self.orphans = sorted(orphans, key=order_key)
        self.parent, self.depth, self.order = {}, {}, []
        tops = sorted(roots + orphans, key=order_key)
        self._walk(tops)
        # 父子关系成环的 Span 从根走不到：从环上（开始最早的）一个 Span 起当顶层节点
        rest = sorted((span for span in self.spans if span.span_id not in self.depth), key=order_key)
        self.cycles = len(rest)
        if rest:
            tops = sorted(tops + self._walk(rest), key=order_key)
        self.top = tops
        self.root = self.roots[0] if self.roots else None
        self.start_ns = min((span.start_ns for span in self.spans), default=0)
        self.end_ns = max((span.end_ns for span in self.spans), default=0)
        self._post_order()

    def _walk(self, tops):
        """从这些节点深度优先走一遍，返回实际作为起点的节点（已经走到过的跳过）"""
        started = []
        for top in tops:
            if top.span_id in self.depth:
                continue
            started.append(top)
            stack = [(top, 0, None)]
            while stack:
                span, depth, parent = stack.pop()
                if span.span_id in self.depth:
                    continue
                self.depth[span.span_id], self.parent[span.span_id] = depth, parent
                self.order.append(span)
                for kid in reversed(self.children.get(span.span_id, ())):
                    if kid.span_id not in self.depth:
                        stack.append((kid, depth + 1, span.span_id))
        return started
    def kids(self, span):
        """DFS 里真正挂在它下面的子 Span（成环时第二次遇到的不算）"""
        return [kid for kid in self.children.get(span.span_id, ()) if self.parent.get(kid.span_id) == span.span_id]

    def _post_order(self):
        self.size, self.error_below, self.self_ns = {}, {}, {}
        for span in reversed(self.order):
            kids = self.kids(span)
            self.size[span.span_id] = 1 + sum(self.size[kid.span_id] for kid in kids)
            self.error_below[span.span_id] = any(kid.is_error or self.error_below[kid.span_id] for kid in kids)
            self.self_ns[span.span_id] = _self_time(span, kids)

    def ancestors(self, span_id):
        result, current = [], self.parent.get(span_id)
        while current is not None and current not in result:
            result.append(current)
            current = self.parent.get(current)
        return result


def _self_time(span, kids):
    """自身耗时 = 耗时 − 子 Span 时间（裁剪到它的范围内）的并集"""
    intervals = sorted((max(kid.start_ns, span.start_ns), min(kid.end_ns, span.end_ns)) for kid in kids)
    covered, current_start, current_end = 0, None, None
    for start, end in intervals:
        if end <= start:
            continue
        if current_end is None or start > current_end:
            if current_end is not None:
                covered += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        covered += current_end - current_start
    return max(0, span.duration_ns - covered)


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------
def outcome_of(root, has_error):
    if root is None:
        return 'unknown'
    status = root.http_status or 0
    if root.is_error or (root.kind == 'server' and status >= 500):
        return 'failed'
    if 400 <= status < 500:
        return 'client_error'
    return 'recovered' if has_error else 'succeeded'


def overview(index, trace_id):
    services, clusters = {}, {}
    for span in index.spans:
        item = services.setdefault(span.service, [0, 0])
        item[0] += 1
        item[1] += span.is_error
        if span.cluster:
            entry = clusters.setdefault(span.cluster['value'], [span.cluster, 0])
            entry[1] += 1
    root = index.root
    has_error = any(span.is_error for span in index.spans)
    data = {
        'trace_id': trace_id,
        'start': iso_ns(index.start_ns) if index.spans else None,
        'duration_ms': _ms(index.end_ns - index.start_ns),
        'span_count': len(index.spans),
        'service_count': len(services),
        'services': [{'name': name, 'span_count': count, 'error_span_count': errors}
                     for name, (count, errors) in sorted(services.items(), key=lambda item: (-item[1][0], item[0]))],
        'clusters': [{**cluster, 'span_count': count}
                     for cluster, count in sorted(clusters.values(), key=lambda item: -item[1])],
        'root': None,
        'roots_count': len(index.roots),
        'orphan_spans': len(index.orphans),
        'outcome': outcome_of(root, has_error),
    }
    if root is not None:
        data['root'] = {'span_id': root.span_id, 'service': root.service, 'name': root.name, 'kind': root.kind,
                        'status': root.status, 'http_status': root.http_status,
                        'duration_ms': _ms(root.duration_ns)}
    return data


# ---------------------------------------------------------------------------
# 关键路径（Jaeger UI 的算法）
# ---------------------------------------------------------------------------
def critical_path(index, root=None):
    """返回 ({span_id: 关键路径上的自身时间 ns}, 总时长 ns)"""
    root = root or index.root or max(index.top, key=lambda span: span.duration_ns, default=None)
    if root is None:
        return {}, 0
    clipped = {root.span_id: (root.start_ns, root.end_ns)}
    kids_by_end = {}
    stack = [root]
    while stack:
        span = stack.pop()
        parent_start, parent_end = clipped[span.span_id]
        kept = []
        for kid in index.kids(span):
            if kid.start_ns >= parent_end or kid.end_ns <= parent_start:
                continue  # 完全在父 Span 的时间范围外：连同子树丢掉
            clipped[kid.span_id] = (max(kid.start_ns, parent_start), min(kid.end_ns, parent_end))
            kept.append(kid)
            stack.append(kid)
        # 按（裁剪后的）结束时间从晚到早；同时结束的按原顺序
        kept.sort(key=lambda kid: -clipped[kid.span_id][1])
        kids_by_end[span.span_id] = kept
    sections = defaultdict(int)
    pointer = defaultdict(int)
    current, bound = root, None
    for _ in range(4 * len(clipped) + 4):
        start, end = clipped[current.span_id]
        limit = end if bound is None else bound
        kids, index_ = kids_by_end[current.span_id], pointer[current.span_id]
        if bound is not None:  # 回到父 Span：只看在 bound 之前结束的子 Span
            while index_ < len(kids) and clipped[kids[index_].span_id][1] >= bound:
                index_ += 1
        lfc = kids[index_] if index_ < len(kids) else None
        if lfc is not None:
            pointer[current.span_id] = index_ + 1
            lfc_end = clipped[lfc.span_id][1]
            if lfc_end != limit:
                sections[current.span_id] += limit - lfc_end
            current, bound = lfc, None
            continue
        pointer[current.span_id] = len(kids)
        if start != limit:
            sections[current.span_id] += limit - start
        if current is root:
            break
        current, bound = index.by_id[index.parent[current.span_id]], start
    return dict(sections), root.duration_ns


def critical_summary(index, sections, total):
    top = sorted(sections.items(), key=lambda item: -item[1])[:CRITICAL_TOP]
    return {'total_ms': _ms(total), 'top': [
        {'id': span_id, 'service': index.by_id[span_id].service, 'name': index.by_id[span_id].name,
         'self_ms': _ms(value), 'pct': round(value * 100 / total, 1) if total else 0.0}
        for span_id, value in top if value > 0]}


# ---------------------------------------------------------------------------
# N+1、最慢、出错
# ---------------------------------------------------------------------------
def n_plus_one(index):
    groups = []
    for parent_id, kids in index.children.items():
        if len(kids) < N_PLUS_ONE_MIN or parent_id not in index.by_id:
            continue
        by_key = defaultdict(list)
        for kid in kids:
            by_key[(kid.service, kid.name)].append(kid)
        parent = index.by_id[parent_id]
        for (service, name), members in by_key.items():
            if len(members) < N_PLUS_ONE_MIN:
                continue
            window = max(m.end_ns for m in members) - min(m.start_ns for m in members)
            total = sum(m.duration_ns for m in members)
            groups.append({
                'parent': {'id': parent.span_id, 'service': parent.service, 'name': parent.name},
                'child': {'service': service, 'name': name},
                'count': len(members), 'window_ms': _ms(window), 'sum_ms': _ms(total),
                'serial': total <= window * 1.1,
                'example_ids': [m.span_id for m in members[:3]],
            })
    groups.sort(key=lambda item: (not item['serial'], -item['sum_ms'], -item['count']))
    return groups[:N_PLUS_ONE_LIMIT]


def slowest(index):
    spans = sorted(index.spans, key=lambda span: -index.self_ns[span.span_id])[:SLOWEST_LIMIT]
    return [{'id': span.span_id, 'service': span.service, 'name': span.name, 'kind': span.kind,
             'duration_ms': _ms(span.duration_ns), 'self_ms': _ms(index.self_ns[span.span_id])} for span in spans]


def _compact(item):
    return {key: value for key, value in item.items() if value not in (None, '', [], {})}


def error_list(index):
    errors = [span for span in index.spans if span.is_error]
    errors.sort(key=lambda span: (-index.depth.get(span.span_id, 0), span.start_ns))
    return {'total': len(errors), 'spans': [_compact({
        'id': span.span_id, 'service': span.service, 'name': span.name, 'kind': span.kind, 'k8s_pod': span.pod,
        'cluster': span.cluster, 'http_status': span.http_status, 'error': span.error_message,
        'depth': index.depth.get(span.span_id, 0)}) for span in errors[:ERROR_LIMIT]]}


# ---------------------------------------------------------------------------
# 根因
# ---------------------------------------------------------------------------
def _walk_waiting(index, span):
    """「在等」：子 Span 的耗时 ≥ 它的 90%（它 ≥ 100ms）就往下走，直到没有这样的子 Span"""
    path = [span]
    while path[-1].duration_ns >= WAIT_MIN_NS:
        kids = index.kids(path[-1])
        best = max(kids, key=lambda kid: kid.duration_ns, default=None)
        if best is None or best.duration_ns < WAIT_RATIO * path[-1].duration_ns:
            break
        path.append(best)
    return path


def _evidence(span):
    evidence = {'duration_ms': _ms(span.duration_ns)}
    if span.http_status:
        evidence['http_status'] = span.http_status
    if span.error_message:
        evidence['error'] = span.error_message
    exc_type, exc_message = exception_info(span)
    if exc_type:
        evidence['exception_type'] = exc_type
    if exc_message:
        evidence['exception_message'] = exc_message[:EXCEPTION_CHARS]
    return evidence


def _peer_text(span):
    return span.peer or '被调方'


def _describe(index, candidate, path):
    final = path[-1]
    kids = index.kids(final)
    item = {'id': final.span_id, 'service': final.service, 'name': final.name, 'kind': final.kind,
            'k8s_pod': final.pod, 'cluster': final.cluster}
    evidence = _evidence(final)
    if len(path) > 1:
        reason = REASON_WAIT
        if final.duration_ns > candidate.duration_ns:
            reason += (f'；它耗时 {_ms(final.duration_ns)} ms，比报错的调用（{_ms(candidate.duration_ns)} ms）还长：'
                       '调用方先超时放弃了，它还在执行')
        if not kids and final.kind in ('client', 'producer'):
            reason += f'；它是对 {_peer_text(final)} 的调用，对方没有记录 Span（可能没接入链路，或请求没处理完）'
        evidence['error_span'] = _compact({
            'id': candidate.span_id, 'service': candidate.service, 'name': candidate.name,
            'duration_ms': _ms(candidate.duration_ns), 'http_status': candidate.http_status,
            'error': candidate.error_message})
    elif not kids and _is_timeout(candidate.error_message):
        reason = REASON_TIMEOUT
    elif not kids and candidate.is_error and candidate.kind in ('client', 'producer'):
        system = candidate.attrs.get('db.system')
        status = candidate.http_status
        if system:
            reason = f'数据库 / 缓存调用出错（{system}），看 error 和异常信息'
        elif status in (502, 503):
            reason = (f'调用 {_peer_text(candidate)} 返回 HTTP {status}，对方没有记录 Span：被调方可能不可用'
                      '（网关 / 负载均衡找不到可用实例或连不上），也可能对方没接入链路')
            item.update(callee_unreachable=True, peer=candidate.peer)
        elif status:
            reason = (f'调用 {_peer_text(candidate)} 返回 HTTP {status}，对方没有记录 Span：可能对方没接入链路，'
                      '或者是网关 / 负载均衡直接返回的')
        else:
            reason = (f'调用 {_peer_text(candidate)} 失败且对方没有记录：被调方可能不可用、在重启、没有可用实例，'
                      '或请求没到达')
            item.update(callee_unreachable=True, peer=candidate.peer)
    elif not candidate.is_error:
        reason = (f'根 Span 返回 HTTP {candidate.http_status}，链路里没有出错的 Span：问题在 {candidate.service} 自身，'
                  '或者它没有记录下游调用')
    else:
        reason = REASON_DEEPEST.format(service=candidate.service)
    item['reason'] = reason
    item['evidence'] = evidence
    return _compact(item)


def root_causes(index, outcome):
    """返回 (候选列表 ≤3, 候选总数, 根因涉及的 Span ID)"""
    candidates = [span for span in index.spans if span.is_error and not index.error_below[span.span_id]]
    if not candidates and outcome == 'failed' and index.root is not None:
        candidates = [index.root]
    candidates.sort(key=lambda span: (span.end_ns, -index.depth.get(span.span_id, 0)))
    results, seen, involved = [], set(), set()
    for candidate in candidates:
        path = _walk_waiting(index, candidate)
        if path[-1].span_id in seen:
            continue
        seen.add(path[-1].span_id)
        if len(results) < ROOT_CAUSE_LIMIT:
            results.append(_describe(index, candidate, path))
            involved.update(span.span_id for span in path)
    return results, len(seen), involved


# ---------------------------------------------------------------------------
# 调用树：折叠、focus、按 max_nodes 裁剪
# ---------------------------------------------------------------------------
class _Item:
    __slots__ = ('span', 'fold', 'depth', 'parent', 'children', 'hidden', 'removed', 'alive')

    def __init__(self, depth, parent, span=None, fold=None):
        self.span, self.fold, self.depth, self.parent = span, fold, depth, parent
        self.children, self.hidden, self.removed, self.alive = [], 0, False, 0


def _fold_children(index, kids, protected):
    """折叠同一父节点下 (service, name, kind) 相同、≥4 个的兄弟：保留第 1 个、最慢的 1 个和受保护的，其余合并"""
    groups = defaultdict(list)
    for kid in kids:
        groups[(kid.service, kid.name, kid.kind)].append(kid)
    folded = {}
    for key, members in groups.items():
        if len(members) < FOLD_MIN:
            continue
        keep = {members[0].span_id, max(members, key=lambda span: span.duration_ns).span_id}
        rest = [span for span in members if span.span_id not in keep and span.span_id not in protected]
        if len(rest) >= 2:
            for span in rest:
                folded[span.span_id] = key
    entries, emitted = [], set()
    for kid in kids:
        key = folded.get(kid.span_id)
        if key is None:
            entries.append(kid)
        elif key not in emitted:
            emitted.add(key)
            members = [span for span in groups[key] if folded.get(span.span_id) == key]
            durations = [span.duration_ns for span in members]
            entries.append({
                'count': len(members), 'service': key[0], 'name': key[1], 'kind': key[2],
                'total_ms': _ms(sum(durations)), 'min_ms': _ms(min(durations)), 'max_ms': _ms(max(durations)),
                'errors': sum(span.is_error for span in members),
                'descendants': sum(index.size[span.span_id] - 1 for span in members),
            })
    return entries


def _build_visible(index, protected, focus=None):
    items = []

    def node(span, depth, parent):
        item = _Item(depth, parent, span=span)
        items.append(item)
        if parent is not None:
            parent.children.append(item)
        return item

    queue = []
    if focus is not None:
        chain = [index.by_id[span_id] for span_id in reversed(index.ancestors(focus.span_id))] + [focus]
        parent = None
        for depth, span in enumerate(chain):
            parent = node(span, depth, parent)
            if span is not focus:  # 路径上的祖先只给通往 focus 的那个子节点，其余记 hidden_children
                parent.hidden = index.size[span.span_id] - 1 - index.size[chain[depth + 1].span_id]
        limit = parent.depth + FOCUS_DEPTH
        queue.append(parent)
    else:
        limit = None
        queue.extend(node(span, 0, None) for span in index.top)
    while queue:
        item = queue.pop()
        kids = index.kids(item.span)
        if not kids:
            continue
        if limit is not None and item.depth >= limit:
            item.hidden += index.size[item.span.span_id] - 1
            continue
        for entry in _fold_children(index, kids, protected):
            if isinstance(entry, dict):
                child = _Item(item.depth + 1, item, fold=entry)
                items.append(child)
                item.children.append(child)
            else:
                queue.append(node(entry, item.depth + 1, item))
    return [item for item in items if item.parent is None], items


def _prune(items, max_nodes, protected):
    """超过 max_nodes：从最深的、不含受保护节点的叶子开始剪（短的先剪），父节点记 hidden_children"""
    count = len(items)
    if count <= max_nodes:
        return
    for item in items:
        item.alive = len(item.children)
    by_depth = defaultdict(list)
    for item in items:
        if item.depth > 0:
            by_depth[item.depth].append(item)
    for depth in sorted(by_depth, reverse=True):
        candidates = [item for item in by_depth[depth] if item.alive == 0 and (
            item.fold is not None or item.span.span_id not in protected)]
        candidates.sort(key=lambda item: item.fold['total_ms'] * 1e6 if item.fold else item.span.duration_ns)
        for item in candidates:
            if count <= max_nodes:
                return
            item.removed = True
            parent = item.parent
            parent.alive -= 1
            parent.hidden += (item.fold['count'] + item.fold['descendants']) if item.fold else 1 + item.hidden
            count -= 1


def _render(index, roots, base_cluster, start_ns, lean=False):
    out = []
    stack = list(reversed(roots))
    while stack:
        item = stack.pop()
        if item.removed:
            continue
        if item.fold is not None:
            fold = {key: value for key, value in item.fold.items() if key != 'descendants' or value}
            out.append({'depth': item.depth, 'folded': fold})
            continue
        span = item.span
        node = {'id': span.span_id, 'depth': item.depth, 'service': span.service, 'name': span.name,
                'kind': span.kind, 'start_ms': _ms(span.start_ns - start_ns), 'duration_ms': _ms(span.duration_ns),
                'self_ms': _ms(index.self_ns[span.span_id])}
        if span.is_error:
            node['status'] = 'error'
        if span.http_status:
            node['http_status'] = span.http_status
        if span.peer and not lean:
            node['peer'] = span.peer
        if span.pod:
            node['k8s_pod'] = span.pod
        if span.cluster and (base_cluster is None or span.cluster['value'] != base_cluster.get('value')):
            node['cluster'] = span.cluster
        if span.error_message and not lean:
            node['error'] = span.error_message[:TREE_ERROR_CHARS]
        if item.hidden:
            node['hidden_children'] = item.hidden
        out.append(node)
        stack.extend(reversed([child for child in item.children if not child.removed]))
    return out


def build_tree(index, protected, max_nodes, focus=None, lean=False):
    roots, items = _build_visible(index, protected, focus)
    _prune(items, max_nodes, protected)
    base = index.root.cluster if index.root is not None else None
    return _render(index, roots, base, index.start_ns, lean)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _notes(index, outcome, cause_total):
    notes = []
    clusters = {span.cluster['value']: span.cluster for span in index.spans if span.cluster}
    if len(clusters) > 1:
        names = '、'.join(f'{value}（{c["env"]}）' if c['env'] and c['env'] != value else value
                         for value, c in clusters.items())
        notes.append(f'链路涉及多个集群：{names}；别的集群里的 Span 要切到那个集群再查 K8S')
    if index.orphans:
        notes.append(f'有 {len(index.orphans)} 个 Span 的父 Span 不在链路里（链路不完整：采样或数据未写完），结论可能不全')
    if index.root is None:
        notes.append('链路里没有根 Span（可能还没写完或被采样掉），outcome 为 unknown')
    elif len(index.roots) > 1:
        notes.append(f'链路里有 {len(index.roots)} 个根 Span，按最早开始的那个算')
    warned = [span for span in index.spans if span.warnings]
    if warned:
        notes.append(f'有 {len(warned)} 个 Span 带 Jaeger 的提示（例如时钟偏差修正），跨服务的先后和耗时可能有偏差；'
                     f'例：{warned[0].warnings[0][:120]}')
    if index.duplicates:
        notes.append(f'有 {index.duplicates} 个重复的 Span ID，只用了第一个')
    if index.cycles:
        notes.append(f'有 {index.cycles} 个 Span 的父子关系成环，当顶层节点处理')
    if outcome == 'recovered':
        notes.append(NOTE_RECOVERED)
    if cause_total > ROOT_CAUSE_LIMIT:
        notes.append(f'共有 {cause_total} 个独立的出错点，root_cause 只列了前 {ROOT_CAUSE_LIMIT} 个')
    return notes


def analyze(spans, trace_id, *, max_nodes=200, focus_span='', extra=None):
    """归一化后的 Span → {trace, tree, tree_stats, analysis, truncated}；extra 合并进 trace（url、time_range_used）"""
    index = Index(spans)
    data_trace = overview(index, trace_id)
    data_trace.update(extra or {})
    outcome = data_trace['outcome']
    causes, cause_total, involved = root_causes(index, outcome)
    sections, total = critical_path(index)
    critical = critical_summary(index, sections, total)
    analysis = {
        'errors': error_list(index),
        'root_cause': causes,
        'critical_path': critical,
        'n_plus_one': n_plus_one(index),
        'slowest': slowest(index),
        'notes': _notes(index, outcome, cause_total),
    }
    focus = index.by_id.get(focus_span) if focus_span else None
    if focus_span and focus is None:
        analysis['notes'].append(f'focus_span {focus_span} 不在这条链路里，给的是整棵调用树')
    # 永远不折叠、不剪掉：出错 Span、关键路径上最耗时的 Span、根因，以及它们的祖先；focus 的路径
    protected = {span.span_id for span in index.spans if span.is_error}
    protected.update(item['id'] for item in critical['top'])
    protected.update(involved)
    if focus is not None:
        protected.add(focus.span_id)
    for span_id in list(protected):
        protected.update(index.ancestors(span_id))
    result = {'trace': data_trace, 'tree': [], 'analysis': analysis, 'truncated': False}
    limits = [max_nodes] + [step for step in SHRINK_STEPS if step < max_nodes]
    for number, limit in enumerate(limits):
        result['tree'] = build_tree(index, protected, limit, focus)
        result['tree_stats'] = _tree_stats(result['tree'], len(index.spans), limit)
        result['truncated'] = number > 0
        if _size(result) <= MAX_OUTPUT_CHARS:
            return result
    result['tree'] = build_tree(index, protected, limits[-1], focus, lean=True)
    result['tree_stats'] = _tree_stats(result['tree'], len(index.spans), limits[-1])
    result['truncated'] = True
    return result


def _tree_stats(tree, total, limit):
    shown = sum(1 for node in tree if 'id' in node)
    return {'shown_spans': shown, 'total_spans': total, 'max_nodes': limit,
            'folded_groups': sum(1 for node in tree if 'folded' in node)}


def _size(result):
    return len(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
