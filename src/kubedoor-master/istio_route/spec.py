"""Istio VirtualService 规格的纯逻辑(不访问数据库、不依赖 aiohttp,便于单测)。

- parse_spec:集群里的 VS spec → 组标准各部分(gateways、http 规则、默认路由)+ hosts + 规则里的域名。
  没建模的字段原样放进 extra / df_extra / extra_spec,渲染时原样带回,采集 → 下发不丢配置。
- 域名条件(match 里的 authority / headers.host)和 hosts 一样按集群保存:组标准规则的 match 是模板,
  域名条件的位置标成 {"$domains": true},各集群在每个域名块里要匹配的域名单独存,下发时按该集群的域名展开。
- render_spec:组标准 + 某个集群的 hosts 和域名 → 下发到该集群的 spec。
- spec_hash / shared_hash / rule_digest:归一化后的哈希。spec_hash 判断「集群现状 = 组标准」;
  shared_hash 不含 hosts;rule_digest 再不算域名的值,判断各集群的规则是否一致(版本 A / B)。
- diff_specs:两份 spec 的结构化差异,http 规则按匹配条件对齐(名称跟着重写走,不是规则的标识),
  只是域名不同的规则单独标出。
- lint_http / lint_domains:规范检查 —— 用了重写就要有名称,没用重写就不要名称(名称用于访问日志展示);
  域名条件里的域名要在本集群的 hosts 里。
- VSScope / find_conflicts:组的 VS 范围(包含 / 忽略规则 + 单个 VS 的手动忽略 / 单独纳入),
  以及同一个 VS 是否也在共用集群的其他组里(同一个 VS 只能属于一个组)。
- build_matrix:组矩阵,每行一个 VS,每列一个集群。
"""

import copy
import difflib
import hashlib
import json
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

API_VERSION = 'networking.istio.io/v1beta1'
ENV_TYPES = ('test', 'prod')
FORWARD_TYPES = ('route', 'delegate')

# VS 名称 / 命名空间:K8S 的 DNS-1123 规则(名称可以带点,如 b2c.courier-int)
NAME_RE = re.compile(r'^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$')
NAMESPACE_RE = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
# 超时是 Go duration,必须带单位(10s、1m、500ms、1.5s),纯数字会被 Istio 拒绝
DURATION_RE = re.compile(r'^\d+(\.\d+)?(ns|us|µs|ms|s|m|h)$')

# 组标准规则 match 模板里的标记:这个条目的域名条件按集群设置
DOMAIN_KEY = '$domains'
# 域名条件的两种写法:authority,或 headers.host(记作 host);都按域名条件处理,原样保留
DOMAIN_FIELDS = ('authority', 'host')
STRING_OPS = ('exact', 'prefix', 'regex')
# 不按某个集群展开时(组标准本身、该集群还没设置)域名条件显示成这个值
DOMAIN_PLACEHOLDER = '<按集群>'

# 组的 VS 范围:包含 / 忽略规则的写法,单个 VS 的手动决定,以及判断结果的原因
PATTERN_TYPES = ('glob', 'regex')        # 通配符(* 任意多个字符,? 一个字符)/ 正则(全匹配)
OVERRIDE_IGNORE = 'ignore'               # 手动忽略
OVERRIDE_INCLUDE = 'include'             # 单独纳入:组规则排除的,在「查看忽略」里取消忽略
OVERRIDE_ACTIONS = (OVERRIDE_IGNORE, OVERRIDE_INCLUDE)
SCOPE_RULE = 'rule'                      # 组规则纳入
SCOPE_INCLUDED = 'include'               # 单独纳入
SCOPE_MANUAL = 'manual'                  # 手动忽略
SCOPE_IGNORE_RULE = 'ignore_rule'        # 匹配了忽略规则
SCOPE_NOT_INCLUDED = 'not_included'      # 一条包含规则都没匹配上

# 集群格子的状态
SYNCED = 'synced'        # 集群现状 = 组标准 + 该集群 hosts
PENDING = 'pending'      # 不一致,但集群里仍是上次下发(或纳管时)的内容:花折这边改了还没下发
DIFF = 'diff'            # 集群里不是上次下发的内容:有人直接改过集群,或纳管后从未下发且不同
MISSING = 'missing'      # 纳入了该集群,但集群里没有这个 VS
INCOMPLETE = 'incomplete'  # 有规则的域名条件还没设置该集群的域名,设置后才能下发
UNKNOWN = 'unknown'      # 该集群还没采集过现状
UNBOUND = 'unbound'      # 组标准没纳入该集群,但集群里有同名 VS
UNMANAGED = 'unmanaged'  # 还没有组标准(未纳管),集群里有
NONE = 'none'            # 都没有


# ---------------------------------------------------------------------------
# 归一化与哈希
# ---------------------------------------------------------------------------
def _clean(value):
    """去掉值为 None / 空列表 / 空字典的键(列表保持顺序),省略和空值视为相同。"""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            item = _clean(item)
            if item is None or item == [] or item == {}:
                continue
            cleaned[key] = item
        return cleaned
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def canonical(value) -> str:
    return json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def spec_hash(spec: Optional[dict]) -> str:
    return hashlib.sha256(canonical(spec or {}).encode('utf-8')).hexdigest()


def shared_part(spec: Optional[dict]) -> dict:
    """组内共用的部分:spec 去掉 hosts。"""
    return {key: value for key, value in (spec or {}).items() if key != 'hosts'}


def shared_hash(spec: Optional[dict]) -> str:
    return spec_hash(shared_part(spec))


def rule_digest(spec: Optional[dict]) -> str:
    """各集群的规则是否一致:不含 hosts,规则里域名条件的值也不算(和 hosts 一样按集群各自维护)。"""
    shared = shared_part(spec)
    if isinstance(shared.get('http'), list):
        shared['http'] = [templated(entry) for entry in shared['http']]
    return spec_hash(shared)


# ---------------------------------------------------------------------------
# 域名条件(按集群)
# ---------------------------------------------------------------------------
def domain_condition(entry) -> Optional[Tuple[dict, dict]]:
    """match 条目 → (域名条件 {field, op, value}, 其余条件)。

    没有域名条件、同时写了 authority 和 headers.host、或不是单个字符串匹配的,返回 None(整条当普通条件原样保留)。"""
    if not isinstance(entry, dict):
        return None
    rest = dict(entry)
    found = []
    if 'authority' in rest:
        found.append(('authority', rest.pop('authority')))
    headers = rest.get('headers')
    if isinstance(headers, dict) and 'host' in headers:
        headers = dict(headers)
        found.append(('host', headers.pop('host')))
        if headers:
            rest['headers'] = headers
        else:
            rest.pop('headers')
    if len(found) != 1:
        return None
    field, cond = found[0]
    if not isinstance(cond, dict) or len(cond) != 1:
        return None
    op, value = next(iter(cond.items()))
    if op not in STRING_OPS or not isinstance(value, str) or not value:
        return None
    return {'field': field, 'op': op, 'value': value}, copy.deepcopy(rest)


def is_domain_block(block) -> bool:
    return isinstance(block, dict) and bool(block.get(DOMAIN_KEY))


def _block_rest(block: dict) -> dict:
    return {key: value for key, value in block.items() if key != DOMAIN_KEY}


def domain_block_count(template) -> int:
    return sum(1 for block in template if is_domain_block(block)) if isinstance(template, list) else 0


def split_match(match) -> Tuple[object, List[List[dict]]]:
    """规则的 match → (模板, 各域名块的域名)。

    相邻、除域名外条件相同的条目合成一个域名块(模板里一项 {...其余条件, "$domains": true});
    没有域名条件的条目原样留在模板里。"""
    if not isinstance(match, list):
        return copy.deepcopy(match), []
    template, lists = [], []
    for entry in match:
        parsed = domain_condition(entry)
        if parsed is None:
            template.append(copy.deepcopy(entry))
            continue
        item, rest = parsed
        if template and is_domain_block(template[-1]) and canonical(_block_rest(template[-1])) == canonical(rest):
            lists[-1].append(item)
        else:
            template.append({**rest, DOMAIN_KEY: True})
            lists.append([item])
    return template, lists


def domain_entry(rest: dict, item: dict) -> dict:
    entry = copy.deepcopy(rest)
    cond = {item['op']: item['value']}
    if item['field'] == 'host':
        entry['headers'] = {**(entry.get('headers') or {}), 'host': cond}
    else:
        entry['authority'] = cond
    return entry


def render_match(template, lists: Optional[List[List[dict]]]):
    """模板 + 某个集群各域名块的域名 → match,每个域名展开成一个条目(多个条目之间是「或」)。

    lists 为 None(组标准本身,或该集群还没设置)时每个域名块显示一个占位条目。"""
    if not isinstance(template, list):
        return copy.deepcopy(template)
    result, index = [], 0
    for block in template:
        if not is_domain_block(block):
            result.append(copy.deepcopy(block))
            continue
        if lists is None:
            items = [{'field': 'authority', 'op': 'exact', 'value': DOMAIN_PLACEHOLDER}]
        else:
            items = lists[index] if index < len(lists) else []
        index += 1
        rest = _block_rest(block)
        result += [domain_entry(rest, item) for item in items]
    return result


def merge_domain_blocks(template, domains: Dict[str, List[List[dict]]]):
    """相邻、除域名外条件相同的域名块合成一块(和 split_match 拆出来的一致),各集群的域名跟着合并。"""
    if not isinstance(template, list):
        return template, domains
    merged, groups, index = [], [], 0
    for block in template:
        if not is_domain_block(block):
            merged.append(copy.deepcopy(block))
            continue
        previous = merged[-1] if merged else None
        if is_domain_block(previous) and canonical(_block_rest(previous)) == canonical(_block_rest(block)):
            groups[-1].append(index)
        else:
            merged.append(copy.deepcopy(block))
            groups.append([index])
        index += 1
    result = {}
    for k8s_name, lists in domains.items():
        result[k8s_name] = [[item for i in group for item in (lists[i] if i < len(lists) else [])]
                            for group in groups]
    return merged, result


def templated(entry):
    """规则去掉域名的值(match 换成模板),用来判断「只是域名不同」。"""
    if not isinstance(entry, dict) or not entry.get('match'):
        return entry
    return {**entry, 'match': split_match(entry['match'])[0]}


# ---------------------------------------------------------------------------
# 解析:集群 spec → 组标准各部分
# ---------------------------------------------------------------------------
def _pop_forward(entry: dict) -> Tuple[Optional[str], object]:
    for kind in FORWARD_TYPES:
        if kind in entry:
            return kind, entry.pop(kind)
    return None, None


def is_default_entry(entry) -> bool:
    """没有匹配条件的规则(match 省略或为空)匹配所有请求,放在最后就是默认路由。"""
    return isinstance(entry, dict) and not entry.get('match')


def split_http(http) -> Tuple[List[dict], Optional[dict]]:
    """http 列表 → (普通规则, 默认路由)。只有最后一条没有 match 的才算默认路由,中间的原样当普通规则。"""
    entries = [entry for entry in (http or []) if isinstance(entry, dict)]
    if entries and is_default_entry(entries[-1]):
        return entries[:-1], entries[-1]
    return entries, None


def parse_route(entry: dict, priority: int) -> dict:
    """规则 → 组标准的规则。match 存模板,这个集群在各域名块里的域名放在 domain_lists(组标准按集群保存)。"""
    entry = copy.deepcopy(entry)
    name = entry.pop('name', None)
    template, domain_lists = split_match(entry.pop('match', None))
    rewrite = entry.pop('rewrite', None)
    forward_type, forward_detail = _pop_forward(entry)
    timeout = entry.pop('timeout', None)
    return {
        'priority': priority,
        'name': name or None,
        'match_rules': template or None,
        'domain_lists': domain_lists,
        'rewrite_rules': rewrite or None,
        'forward_type': forward_type,
        'forward_detail': forward_detail,
        'timeout': timeout or None,
        'extra': entry or None,
    }


def parse_default(entry: Optional[dict]) -> dict:
    result = {'df_forward_type': None, 'df_forward_detail': None, 'df_forward_timeout': None, 'df_extra': None}
    if entry is None:
        return result
    entry = copy.deepcopy(entry)
    entry.pop('match', None)
    forward_type, forward_detail = _pop_forward(entry)
    timeout = entry.pop('timeout', None)
    result.update(
        df_forward_type=forward_type,
        df_forward_detail=forward_detail,
        df_forward_timeout=timeout or None,
        df_extra=entry or None,
    )
    return result


def parse_spec(spec: Optional[dict]) -> dict:
    """返回 {'hosts': [...], 'vs': {gateways, df_*, extra_spec}, 'routes': [...]};routes 带这个集群的 domain_lists。"""
    spec = copy.deepcopy(spec) if isinstance(spec, dict) else {}
    hosts = list(spec.pop('hosts', None) or [])
    gateways = list(spec.pop('gateways', None) or [])
    entries, default = split_http(spec.pop('http', None))
    vs = {'gateways': gateways, 'extra_spec': spec or None, **parse_default(default)}
    routes = [parse_route(entry, (index + 1) * 10) for index, entry in enumerate(entries)]
    return {'hosts': hosts, 'vs': vs, 'routes': routes}


# ---------------------------------------------------------------------------
# 渲染:组标准 + 集群 hosts → spec
# ---------------------------------------------------------------------------
def sort_routes(routes: Iterable[dict]) -> List[dict]:
    return sorted(routes, key=lambda route: (route.get('priority') or 0, route.get('id') or 0))


def route_domains(route: dict, k8s_name: Optional[str]) -> Optional[List[List[dict]]]:
    """规则在某个集群的域名;没指定集群或该集群还没设置时返回 None。"""
    if k8s_name is None:
        return None
    return (route.get('domains') or {}).get(k8s_name)


def render_route(route: dict, k8s_name: Optional[str] = None) -> dict:
    entry = {}
    if route.get('name'):
        entry['name'] = route['name']
    if route.get('match_rules'):
        entry['match'] = render_match(route['match_rules'], route_domains(route, k8s_name))
    if route.get('rewrite_rules'):
        entry['rewrite'] = copy.deepcopy(route['rewrite_rules'])
    if route.get('forward_type') in FORWARD_TYPES and route.get('forward_detail'):
        entry[route['forward_type']] = copy.deepcopy(route['forward_detail'])
    if route.get('timeout'):
        entry['timeout'] = route['timeout']
    for key, value in (route.get('extra') or {}).items():
        entry.setdefault(key, copy.deepcopy(value))
    return entry


def render_default(vs: dict) -> Optional[dict]:
    entry = {}
    if vs.get('df_forward_type') in FORWARD_TYPES and vs.get('df_forward_detail'):
        entry[vs['df_forward_type']] = copy.deepcopy(vs['df_forward_detail'])
    if not entry and not vs.get('df_extra'):
        return None
    if vs.get('df_forward_timeout'):
        entry['timeout'] = vs['df_forward_timeout']
    for key, value in (vs.get('df_extra') or {}).items():
        entry.setdefault(key, copy.deepcopy(value))
    return entry


def render_spec(vs: dict, routes: Iterable[dict], hosts: Optional[Iterable[str]],
                k8s_name: Optional[str] = None) -> dict:
    """组标准 + hosts → spec;给了集群时规则里的域名条件用该集群的域名,否则显示占位。"""
    spec = {'hosts': list(hosts or [])}
    if vs.get('gateways'):
        spec['gateways'] = list(vs['gateways'])
    http = [render_route(route, k8s_name) for route in sort_routes(routes)]
    default = render_default(vs)
    if default:
        http.append(default)
    if http:
        spec['http'] = http
    for key, value in (vs.get('extra_spec') or {}).items():
        spec.setdefault(key, copy.deepcopy(value))
    return spec


def manifest(namespace: str, name: str, spec: dict) -> dict:
    return {
        'apiVersion': API_VERSION,
        'kind': 'VirtualService',
        'metadata': {'name': name, 'namespace': namespace},
        'spec': spec,
    }


# kubectl apply 记下的上次配置:整个对象的 JSON 串,很长,内容和 spec 重复
LAST_APPLIED_ANNOTATION = 'kubectl.kubernetes.io/last-applied-configuration'


def clean_live(obj: dict) -> dict:
    """查看集群现状用:原样保留,只去掉 metadata.managedFields 和 last-applied 注解(注解删空了就整个去掉)。"""
    obj = copy.deepcopy(obj)
    metadata = obj.get('metadata')
    if isinstance(metadata, dict):
        metadata.pop('managedFields', None)
        annotations = metadata.get('annotations')
        if isinstance(annotations, dict):
            annotations.pop(LAST_APPLIED_ANNOTATION, None)
            if not annotations:
                metadata.pop('annotations')
    return obj


# ---------------------------------------------------------------------------
# 规则描述、规范检查
# ---------------------------------------------------------------------------
def _conditions(field, spec):
    if not isinstance(spec, dict):
        return []
    return ['域名按集群' if value == DOMAIN_PLACEHOLDER else f'{field} {op} {value}' for op, value in spec.items()]


def describe_match(match) -> str:
    """规则的简短描述,如 `uri prefix /a/ & header x-env exact gray`。"""
    if not match:
        return '无匹配条件'
    parts = []
    for cond in match if isinstance(match, list) else [match]:
        if not isinstance(cond, dict):
            continue
        items = _conditions('uri', cond.get('uri')) + _conditions('authority', cond.get('authority'))
        for key, spec in (cond.get('headers') or {}).items():
            items += _conditions(f'header {key}', spec)
        if cond.get(DOMAIN_KEY):
            items.append('域名按集群')
        items += [key for key in cond if key not in ('uri', 'authority', 'headers', DOMAIN_KEY)]
        parts.append(' & '.join(items) or '空条件')
    return ' | '.join(parts)


def route_label(entry: dict) -> str:
    return entry.get('name') or describe_match(entry.get('match'))


def forward_label(entry: dict) -> str:
    """转发目标的简短描述:第一个目标的服务(去掉 .svc.cluster.local),委托写下级 VS。"""
    delegate = entry.get('delegate')
    if isinstance(delegate, dict):
        return f"委托 {delegate.get('namespace', '')}/{delegate.get('name', '')}"
    targets = entry.get('route')
    if isinstance(targets, list) and targets and isinstance(targets[0], dict):
        return str((targets[0].get('destination') or {}).get('host') or '').replace('.svc.cluster.local', '')
    return ''


def route_issue(name, rewrite) -> Optional[str]:
    """规则名称跟着重写走:用了重写就要有名称(访问日志里看原始前缀),没用重写就不要名称。"""
    if rewrite and not name:
        return '用了重写但没有名称'
    if name and not rewrite:
        return '没用重写却设置了名称'
    return None


def lint_http(http) -> List[str]:
    issues = []
    for index, entry in enumerate(entry for entry in (http or []) if isinstance(entry, dict)):
        issue = route_issue(entry.get('name'), entry.get('rewrite'))
        if issue:
            issues.append(f'第 {index + 1} 条规则({route_label(entry)}){issue}')
    return issues


def _host_covers(pattern: str, value: str) -> bool:
    value = re.sub(r':\d+$', '', value)
    if pattern in (value, '*'):
        return True
    return pattern.startswith('*.') and value.endswith(pattern[1:])


def lint_domains(spec: Optional[dict], where: str = '') -> List[str]:
    """域名条件里精确匹配的域名要在这个 VS 的 hosts 里,不在的话请求根本到不了这条规则。"""
    spec = spec or {}
    hosts = [h for h in spec.get('hosts') or [] if isinstance(h, str)]
    issues = []
    for index, entry in enumerate(entry for entry in (spec.get('http') or []) if isinstance(entry, dict)):
        match = entry.get('match')
        values = []
        for cond in match if isinstance(match, list) else []:
            parsed = domain_condition(cond)
            if parsed and parsed[0]['op'] == 'exact' and parsed[0]['value'] != DOMAIN_PLACEHOLDER:
                values.append(parsed[0]['value'])
        outside = [v for v in dict.fromkeys(values) if not any(_host_covers(h, v) for h in hosts)]
        if outside:
            issues.append(f"{where}第 {index + 1} 条规则({route_label(entry)})的域名 {'、'.join(outside)} "
                          '不在 hosts 里')
    return issues


# ---------------------------------------------------------------------------
# 对比
# ---------------------------------------------------------------------------
def route_key(entry: dict) -> str:
    """规则对齐用的标识:匹配条件。"""
    return canonical(entry.get('match') or [])


def rule_key(entry: dict) -> str:
    """不算域名值的匹配条件:两条规则这个一样,一条的域名才能套到另一条上。"""
    return canonical(split_match(entry.get('match') or [])[0])


def forward_key(entry: dict) -> str:
    """转发目标:各目标的 destination(不算权重、不分先后),或委托的下级 VS、重定向等其他写法。"""
    targets = entry.get('route')
    if isinstance(targets, list):
        return canonical(sorted({canonical(t.get('destination')) for t in targets if isinstance(t, dict)}))
    return canonical({key: entry[key] for key in ('delegate', 'redirect', 'directResponse') if key in entry})


def domain_rule_key(entry: dict) -> str:
    """各集群只是域名不同的同一条规则:不算域名值的匹配条件一样,转发目标也一样。

    只看匹配条件不够:只有域名条件的几条规则去掉域名后一模一样,只能按位置对,
    某个集群少一条这样的规则时,后面规则的域名就对到别的规则上了。"""
    return canonical([rule_key(entry), forward_key(entry)])


def _merge_moved(rows: List[dict], left_keys: List[str], right_keys: List[str]) -> List[dict]:
    """没对齐上、但两边都有的同一条规则:合并成一行,标记顺序不同。"""
    pending = {}
    for row in rows:
        if row['right_index'] is None:
            pending.setdefault(left_keys[row['left_index']], []).append(row)
    merged = []
    for row in rows:
        if row['left_index'] is None:
            candidates = pending.get(right_keys[row['right_index']])
            if candidates:
                partner = candidates.pop(0)
                partner['right_index'] = row['right_index']
                partner['moved'] = True
                continue
        merged.append(row)
    return merged


def align_rules(left: List[dict], right: List[dict]) -> List[dict]:
    """两组规则对齐,返回 [{left_index, right_index, moved?}]。

    先按匹配条件对齐;对不上的一段里再按 domain_rule_key 对齐(各集群只是域名不同的同一条规则);
    最后两边都有、但位置对不上的合并成一行,标记 moved。"""
    keys = [route_key(e) for e in left], [route_key(e) for e in right]
    domain_keys = [domain_rule_key(e) for e in left], [domain_rule_key(e) for e in right]
    rows = []
    matcher = difflib.SequenceMatcher(a=keys[0], b=keys[1], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            rows += [{'left_index': i, 'right_index': j} for i, j in zip(range(i1, i2), range(j1, j2))]
            continue
        inner = difflib.SequenceMatcher(a=domain_keys[0][i1:i2], b=domain_keys[1][j1:j2], autojunk=False)
        for tag2, a1, a2, b1, b2 in inner.get_opcodes():
            if tag2 == 'equal':
                rows += [{'left_index': i1 + a, 'right_index': j1 + b} for a, b in zip(range(a1, a2), range(b1, b2))]
            else:
                rows += [{'left_index': i1 + a, 'right_index': None} for a in range(a1, a2)]
                rows += [{'left_index': None, 'right_index': j1 + b} for b in range(b1, b2)]
    rows = _merge_moved(rows, *keys)
    return _merge_moved(rows, *domain_keys)


def diff_http(left: List[dict], right: List[dict]) -> List[dict]:
    """两组规则按匹配条件对齐。op:same / domains(只是域名不同)/ changed / left_only / right_only;
    同一条规则两边都有但位置对不上的标 moved(规则顺序不同,命中结果可能不同)。"""
    result = []
    for row in align_rules(left, right):
        i, j = row['left_index'], row['right_index']
        entry_left = left[i] if i is not None else None
        entry_right = right[j] if j is not None else None
        if entry_left is None:
            op = 'right_only'
        elif entry_right is None:
            op = 'left_only'
        elif canonical(entry_left) == canonical(entry_right):
            op = 'same'
        elif canonical(templated(entry_left)) == canonical(templated(entry_right)):
            op = 'domains'
        else:
            op = 'changed'
        result.append({
            'op': op,
            'moved': bool(row.get('moved')),
            'label': route_label(entry_left or entry_right),
            'left_index': i,
            'right_index': j,
            'left': entry_left,
            'right': entry_right,
        })
    return result


def diff_specs(left: Optional[dict], right: Optional[dict]) -> dict:
    left, right = left or {}, right or {}
    left_routes, left_default = split_http(left.get('http'))
    right_routes, right_default = split_http(right.get('http'))
    left_hosts, right_hosts = list(left.get('hosts') or []), list(right.get('hosts') or [])
    left_extra = {k: v for k, v in left.items() if k not in ('hosts', 'gateways', 'http')}
    right_extra = {k: v for k, v in right.items() if k not in ('hosts', 'gateways', 'http')}
    routes = diff_http(left_routes, right_routes)
    return {
        'same': spec_hash(left) == spec_hash(right),
        'shared_same': shared_hash(left) == shared_hash(right),
        # 规则一致:只有 hosts 和规则里的域名不同(这两样都按集群各自维护)
        'rules_same': rule_digest(left) == rule_digest(right),
        'hosts': {
            'same': left_hosts == right_hosts,
            'left_only': [h for h in left_hosts if h not in right_hosts],
            'right_only': [h for h in right_hosts if h not in left_hosts],
        },
        'gateways': {
            'same': canonical(left.get('gateways') or []) == canonical(right.get('gateways') or []),
            'left': left.get('gateways') or [],
            'right': right.get('gateways') or [],
        },
        'default': {
            'same': canonical(left_default) == canonical(right_default),
            'left': left_default,
            'right': right_default,
        },
        'extra': {
            'same': canonical(left_extra) == canonical(right_extra),
            'left': left_extra,
            'right': right_extra,
        },
        'routes': routes,
        'summary': {
            'left_only': sum(1 for r in routes if r['op'] == 'left_only'),
            'right_only': sum(1 for r in routes if r['op'] == 'right_only'),
            'changed': sum(1 for r in routes if r['op'] == 'changed'),
            'domains': sum(1 for r in routes if r['op'] == 'domains'),
            'moved': sum(1 for r in routes if r['moved']),
        },
    }


# ---------------------------------------------------------------------------
# 组的 VS 范围:包含 / 忽略规则 + 单个 VS 的手动忽略 / 单独纳入;跨组冲突
# ---------------------------------------------------------------------------
def compile_pattern(item: dict):
    """{'type': 'glob' | 'regex', 'pattern': ...} → 全匹配用的正则;正则写错抛 re.error。"""
    pattern = item['pattern']
    if item.get('type') == 'regex':
        return re.compile(pattern)
    return re.compile(''.join('.*' if ch == '*' else '.' if ch == '?' else re.escape(ch) for ch in pattern))


def pattern_matches(item: dict, compiled, namespace: str, name: str) -> bool:
    """规则里有 / 时匹配「命名空间/名称」,否则只匹配名称。"""
    target = f'{namespace}/{name}' if '/' in item['pattern'] else name
    return compiled.fullmatch(target) is not None


class VSScope:
    """组的 VS 范围。先看单个 VS 的手动决定,再看组规则:匹配忽略规则的排除,
    包含规则为空表示全部,否则至少匹配一条包含规则。"""

    def __init__(self, include=(), ignore=(), overrides: Optional[Dict[tuple, str]] = None):
        self.include = [(item, compile_pattern(item)) for item in include or ()]
        self.ignore = [(item, compile_pattern(item)) for item in ignore or ()]
        self.overrides = overrides or {}  # {(namespace, name): 'ignore' | 'include'}

    def rule(self, namespace: str, name: str) -> dict:
        """只按组规则判断:{'visible', 'reason', 'pattern'}。"""
        for item, compiled in self.ignore:
            if pattern_matches(item, compiled, namespace, name):
                return {'visible': False, 'reason': SCOPE_IGNORE_RULE, 'pattern': item['pattern']}
        if not self.include:
            return {'visible': True, 'reason': SCOPE_RULE, 'pattern': None}
        for item, compiled in self.include:
            if pattern_matches(item, compiled, namespace, name):
                return {'visible': True, 'reason': SCOPE_RULE, 'pattern': item['pattern']}
        return {'visible': False, 'reason': SCOPE_NOT_INCLUDED, 'pattern': None}

    def classify(self, namespace: str, name: str) -> dict:
        action = self.overrides.get((namespace, name))
        if action == OVERRIDE_IGNORE:
            return {'visible': False, 'reason': SCOPE_MANUAL, 'pattern': None}
        if action == OVERRIDE_INCLUDE:
            return {'visible': True, 'reason': SCOPE_INCLUDED, 'pattern': None}
        return self.rule(namespace, name)

    def visible(self, namespace: str, name: str) -> bool:
        return self.classify(namespace, name)['visible']


def find_conflicts(group_id: Optional[int], clusters: List[str], keys: Iterable[tuple], others: List[dict],
                   present_by_cluster: Dict[str, set]) -> Dict[tuple, List[dict]]:
    """同一个 VS 只能属于一个组:keys(本组的 VS)里,也在和本组共用集群的其他组里的。

    其他组「有」一个 VS = 它的组标准或它任一集群的现状里有,且没被它的规则 / 手动忽略排除。
    others:[{'id', 'name', 'clusters': [k8s_name], 'scope': VSScope, 'standards': {(namespace, name)}}];
    返回 {(namespace, name): [{'group_id', 'group', 'clusters': 共用的集群}]}。"""
    keys = list(keys)
    result: Dict[tuple, List[dict]] = {}
    for other in others:
        if other['id'] == group_id:
            continue
        theirs = set(other['clusters'])
        shared = [c for c in clusters if c in theirs]
        if not shared:
            continue
        present = set(other.get('standards') or ())
        for k8s_name in other['clusters']:
            present |= present_by_cluster.get(k8s_name, set())
        for key in keys:
            if key in present and other['scope'].visible(*key):
                result.setdefault(key, []).append({'group_id': other['id'], 'group': other['name'], 'clusters': shared})
    return result


# ---------------------------------------------------------------------------
# 状态、版本、组矩阵
# ---------------------------------------------------------------------------
def binding_status(rendered: dict, applied_hash: Optional[str], live: Optional[dict], collected: bool) -> str:
    if live is None:
        return MISSING if collected else UNKNOWN
    live_hash = spec_hash(live)
    if live_hash == spec_hash(rendered):
        return SYNCED
    if applied_hash and live_hash == applied_hash:
        return PENDING
    return DIFF


def _letter(index: int) -> str:
    return chr(ord('A') + index) if index < 26 else f'V{index + 1}'


def version_labels(specs: Dict[str, dict], order: List[str]) -> Tuple[Dict[str, str], List[dict]]:
    """按规则(不含 hosts,不算规则里域名的值)把各集群的 spec 分成版本 A / B / C...,集群多的版本排前面。"""
    groups: Dict[str, List[str]] = {}
    for k8s_name in order:
        if k8s_name in specs:
            groups.setdefault(rule_digest(specs[k8s_name]), []).append(k8s_name)
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), order.index(item[1][0])))
    labels, versions = {}, []
    for index, (digest, names) in enumerate(ranked):
        label = _letter(index)
        versions.append({'label': label, 'hash': digest, 'clusters': names})
        labels.update({name: label for name in names})
    return labels, versions


def standard_spec(standard: dict, hosts: Optional[Iterable[str]] = None, k8s_name: Optional[str] = None) -> dict:
    """组标准 → spec;给了集群时用该集群的域名(hosts 由调用方给),否则规则里的域名条件显示占位。"""
    return render_spec(standard, standard.get('routes') or [], hosts, k8s_name)


def missing_domains(standard: dict, k8s_name: str) -> List[str]:
    """该集群还没设置域名、或一个域名都没选的规则,按「第 N 条规则(描述)」列出;有的话不能下发。"""
    missing = []
    for index, route in enumerate(sort_routes(standard.get('routes') or [])):
        template = route.get('match_rules')
        count = domain_block_count(template)
        if not count:
            continue
        lists = route_domains(route, k8s_name)
        if lists is None or len(lists) != count or not render_match(template, lists):
            entry = render_route(route)
            target = forward_label(entry)
            missing.append(f"第 {index + 1} 条规则({route_label(entry)}{' → ' + target if target else ''})")
    return missing


def cluster_status(standard: dict, binding: dict, k8s_name: str, live: Optional[dict],
                   collected: bool) -> Tuple[str, dict]:
    """组标准纳入的某个集群:(状态, 要下发的 spec)。规则的域名没设置全时状态是 INCOMPLETE。"""
    rendered = standard_spec(standard, binding.get('hosts'), k8s_name)
    if missing_domains(standard, k8s_name):
        return INCOMPLETE, rendered
    return binding_status(rendered, binding.get('applied_hash'), live, collected), rendered


def aligned_domains(entries: List[dict], live: Optional[dict]) -> Dict[int, List[List[dict]]]:
    """一组规则(不含默认路由)和集群现状对齐,取现状里对应规则的域名:{规则下标: 各域名块的域名}。

    只取不算域名值时匹配条件一样的;对不上的规则、现状里没有域名条件的不返回。"""
    if not isinstance(live, dict):
        return {}
    live_entries, _ = split_http(live.get('http'))
    result = {}
    for row in align_rules(entries, live_entries):
        i, j = row['left_index'], row['right_index']
        if i is None or j is None or rule_key(entries[i]) != rule_key(live_entries[j]):
            continue
        lists = split_match(live_entries[j].get('match') or [])[1]
        if lists and not any(item['value'] == DOMAIN_PLACEHOLDER for items in lists for item in items):
            result[i] = lists
    return result


def build_matrix(clusters: List[dict], standards: List[dict], snapshots: Dict[tuple, dict]) -> List[dict]:
    """组矩阵。

    clusters:当前用户能看到的组内集群(按显示顺序),含 k8s_name / env_type / collected_at;
    standards:组标准,含 routes(带各集群的 domains)和 bindings({k8s_name: {hosts, applied_hash, ...}});
    snapshots:{(k8s_name, namespace, name): spec}。
    """
    names = [c['k8s_name'] for c in clusters]
    collected = {c['k8s_name']: bool(c.get('collected_at')) for c in clusters}
    env_types = {c['k8s_name']: c.get('env_type') for c in clusters}
    by_key = {(s['namespace'], s['name']): s for s in standards}
    keys = set(by_key) | {(ns, name) for (k8s_name, ns, name) in snapshots if k8s_name in collected}

    rows = []
    for namespace, name in sorted(keys):
        standard = by_key.get((namespace, name))
        live = {k: snapshots[(k, namespace, name)] for k in names if (k, namespace, name) in snapshots}
        labels, versions = version_labels(live, names)
        row = {'namespace': namespace, 'name': name, 'vs_id': None, 'versions': versions, 'cells': {}}
        if standard:
            shared = standard_spec(standard)
            digest = rule_digest(shared)
            row.update(
                vs_id=standard['id'],
                standard_version=next((v['label'] for v in versions if v['hash'] == digest), None),
                gateways=list(standard.get('gateways') or []),
                route_count=len(standard.get('routes') or []),
                has_default=render_default(standard) is not None,
                lint=lint_http(shared.get('http')),
                updated_at=standard.get('updated_at'),
                updated_by=standard.get('updated_by') or '',
            )
        else:
            base = live[versions[0]['clusters'][0]] if versions else {}
            entries, default = split_http(base.get('http'))
            row.update(
                standard_version=None,
                gateways=list(base.get('gateways') or []),
                route_count=len(entries),
                has_default=default is not None,
                lint=lint_http(base.get('http')),
                updated_at=None,
                updated_by='',
            )

        bindings = (standard or {}).get('bindings') or {}
        for k8s_name in names:
            spec = live.get(k8s_name)
            binding = bindings.get(k8s_name)
            cell = {'in_cluster': spec is not None, 'version': labels.get(k8s_name), 'bound': binding is not None}
            if binding is not None:
                status, rendered = cluster_status(standard, binding, k8s_name, spec, collected[k8s_name])
                if status != INCOMPLETE:
                    row['lint'] += lint_domains(rendered, f'{k8s_name}:')
                cell.update(
                    hosts=list(binding.get('hosts') or []),
                    status=status,
                    applied_at=binding.get('applied_at'),
                    applied_by=binding.get('applied_by') or '',
                )
            elif spec is not None:
                if not standard:
                    row['lint'] += lint_domains(spec, f'{k8s_name}:')
                cell.update(hosts=list(spec.get('hosts') or []), status=UNBOUND if standard else UNMANAGED)
            else:
                cell.update(hosts=[], status=NONE)
            row['cells'][k8s_name] = cell

        # 覆盖检查:只在测试集群有、生产集群都没有(或反过来)
        present = [k for k in names if row['cells'][k]['bound'] or row['cells'][k]['in_cluster']]
        has_test = any(env_types.get(k) == 'test' for k in present)
        has_prod = any(env_types.get(k) == 'prod' for k in present)
        row['coverage'] = 'test_only' if has_test and not has_prod else 'prod_only' if has_prod and not has_test else ''
        row['status_count'] = dict(Counter(cell['status'] for cell in row['cells'].values()))
        rows.append(row)
    return rows
