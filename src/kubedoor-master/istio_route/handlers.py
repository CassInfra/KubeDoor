"""Istio VirtualService 管理接口(/api/istio/*),设计见 README.md。

权限(auth/policy.py 登记):查看按集群读权限,看不到的集群不返回;所有修改只允许管理员。
集群里的 VS 只经这里读写:master 调 agent 现有的 /api/agent/istio/vs(列出)和 /api/agent/istio/vs/apply(下发),
下发内容由 master 按组标准生成,不接受前端传来的 spec;浏览器不能经通用转发直接调这两个 agent 接口。
"""

import asyncio
import functools
import json
from datetime import datetime
from typing import Dict, List, Optional, Union

from aiohttp import web
from loguru import logger
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from auth import audit, client_ip, principal_of
from . import spec as S
from . import store
from .store import IstioError

AGENT_LIST_PATH = '/api/agent/istio/vs'
AGENT_APPLY_PATH = '/api/agent/istio/vs/apply'
AGENT_TIMEOUT = 60

_call_agent = None  # register_routes 时注入 kubedoor-master 的 call_agent_api


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'{type(value).__name__} 不能序列化为 JSON')


def _ok(data=None, **extra):
    body = {'success': True, **extra}
    if data is not None:
        body['data'] = data
    return web.json_response(body, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=_json_default))


def _fail(message, status=400):
    return web.json_response({'success': False, 'error': message}, status=status)


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = '.'.join(str(x) for x in err.get('loc', ()))
        msg = str(err.get('msg', '校验失败')).replace('Value error, ', '')
        parts.append(f'{loc}: {msg}' if loc else msg)
    return '；'.join(parts)


def handler(func):
    @functools.wraps(func)
    async def wrapper(request):
        try:
            return await func(request)
        except IstioError as exc:
            return _fail(exc.message, exc.status)
        except ValidationError as exc:
            return _fail(_validation_message(exc))
        except Exception as exc:
            logger.exception(f'{request.method} {request.path} 失败: {exc}')
            return _fail(f'服务器内部错误: {exc}', 500)

    return wrapper


def _int(value, name) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise IstioError(f'缺少参数或格式不正确: {name}')


async def _body(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise IstioError('请求体不是有效的 JSON')
    if not isinstance(data, dict):
        raise IstioError('请求体必须是 JSON 对象')
    return data


def _username(request) -> str:
    return principal_of(request).username


async def _group(group_id: int) -> dict:
    group = await store.get_group(group_id)
    if group is None:
        raise IstioError('Istio 组不存在', 404)
    return group


def _visible(request, group: dict) -> List[dict]:
    """组里当前用户能看到的集群;一个都看不到就拒绝。"""
    principal = principal_of(request)
    clusters = [c for c in group['clusters'] if principal.can_read(c['k8s_name'])]
    if not clusters:
        raise IstioError('没有该 Istio 组内任何集群的访问权限', 403)
    return clusters


def _members(group: dict, names, what='集群') -> None:
    members = {c['k8s_name'] for c in group['clusters']}
    outside = [name for name in names if name not in members]
    if outside:
        raise IstioError(f"{what} {', '.join(outside)} 不在组 {group['name']} 里")


async def _standard(vs_id: int) -> dict:
    standard = await store.get_standard(vs_id)
    if standard is None:
        raise IstioError('VirtualService 不存在', 404)
    return standard


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
def _timeout(value: Optional[str]) -> Optional[str]:
    """Istio 的 timeout 是 Go duration,必须带单位(10s、1m、500ms),纯数字会被 Istio 拒绝。"""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not S.DURATION_RE.match(value):
        raise ValueError(f"超时格式错误：'{value}' 必须带时间单位(如 10s、1m、500ms)")
    return value


def _hosts(values: List[str]) -> List[str]:
    hosts = []
    for value in values:
        value = (value or '').strip()
        if value and value not in hosts:
            hosts.append(value)
    if not hosts:
        raise ValueError('至少要有一个 host')
    return hosts


class GroupCluster(BaseModel):
    k8s_name: str = Field(min_length=1, max_length=255)
    env_type: str = 'test'
    remark: str = Field(default='', max_length=255)

    @field_validator('env_type')
    @classmethod
    def _env_type(cls, value):
        if value not in S.ENV_TYPES:
            raise ValueError('集群类型只能是 test(测试)或 prod(生产)')
        return value


class GroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ''
    clusters: List[GroupCluster] = []

    @field_validator('name')
    @classmethod
    def _name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('组名不能为空')
        return value

    @model_validator(mode='after')
    def _unique(self):
        names = [c.k8s_name for c in self.clusters]
        if len(names) != len(set(names)):
            raise ValueError('集群不能重复')
        return self


class VSFields(BaseModel):
    """组标准共用部分里可以直接编辑的字段:gateways、默认路由。"""

    gateways: List[str] = []
    df_forward_type: Optional[str] = None
    df_forward_detail: Optional[Union[Dict, List[Dict]]] = None
    df_forward_timeout: Optional[str] = None

    @field_validator('gateways')
    @classmethod
    def _gateways(cls, values):
        return [v.strip() for v in values if v and v.strip()]

    @field_validator('df_forward_timeout')
    @classmethod
    def _df_timeout(cls, value):
        return _timeout(value)

    @model_validator(mode='after')
    def _default_route(self):
        if not self.df_forward_type:
            self.df_forward_type, self.df_forward_detail, self.df_forward_timeout = None, None, None
        elif self.df_forward_type not in S.FORWARD_TYPES:
            raise ValueError('默认路由类型只能是 route 或 delegate')
        elif not self.df_forward_detail:
            raise ValueError('默认路由缺少转发目标')
        return self


class ClusterHosts(BaseModel):
    k8s_name: str = Field(min_length=1)
    hosts: List[str]

    @field_validator('hosts')
    @classmethod
    def _hosts(cls, values):
        return _hosts(values)


class DomainItem(BaseModel):
    """规则里的一个域名条件:field 是 authority 或 host(headers.host),op 是 exact / prefix / regex。"""

    field: str = 'authority'
    op: str = 'exact'
    value: str

    @field_validator('field')
    @classmethod
    def _field(cls, value):
        if value not in S.DOMAIN_FIELDS:
            raise ValueError('域名条件只能写在 authority 或 host(headers.host)上')
        return value

    @field_validator('op')
    @classmethod
    def _op(cls, value):
        if value not in S.STRING_OPS:
            raise ValueError('域名的匹配方式只能是 exact / prefix / regex')
        return value

    @field_validator('value')
    @classmethod
    def _value(cls, value):
        value = (value or '').strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError('域名不能为空,也不能包含空格')
        if value == S.DOMAIN_PLACEHOLDER:
            raise ValueError('请填写实际的域名')
        return value


# 一条规则在某个集群的域名:按模板里域名块的顺序,每块一组
DomainLists = List[List[DomainItem]]


def _domain_lists(lists: DomainLists) -> List[List[dict]]:
    """→ 入库的格式,每个域名块里去掉重复的域名。"""
    result = []
    for items in lists:
        seen, block = set(), []
        for item in items:
            if (item.field, item.op, item.value) not in seen:
                seen.add((item.field, item.op, item.value))
                block.append(item.model_dump())
        result.append(block)
    return result


class VSCreateRequest(VSFields):
    group_id: int
    namespace: str
    name: str
    clusters: List[ClusterHosts] = Field(min_length=1)

    @field_validator('namespace')
    @classmethod
    def _namespace(cls, value):
        value = value.strip()
        if not S.NAMESPACE_RE.match(value):
            raise ValueError('命名空间只能包含小写字母、数字和中横线')
        return value

    @field_validator('name')
    @classmethod
    def _name(cls, value):
        value = value.strip()
        if not S.NAME_RE.match(value):
            raise ValueError('VS 名称只能包含小写字母、数字、中横线和点,且以字母或数字开头结尾')
        return value


class BindingRequest(BaseModel):
    vs_id: int
    k8s_name: str = Field(min_length=1)
    hosts: List[str]
    # 该集群在各规则里的域名:{规则 id: 各域名块的域名};不传就不改
    domains: Optional[Dict[int, DomainLists]] = None

    @field_validator('hosts')
    @classmethod
    def _hosts(cls, values):
        return _hosts(values)


class RouteRequest(BaseModel):
    name: Optional[str] = None
    match_rules: List[Dict]
    rewrite_rules: Optional[Dict] = None
    forward_type: str
    forward_detail: Union[Dict, List[Dict]]
    timeout: Optional[str] = None
    priority: Optional[int] = None
    # match_rules 里 {"$domains": true} 的条目是域名块,各集群在每块里要匹配的域名:{k8s_name: [[域名, ...], ...]}
    domains: Dict[str, DomainLists] = {}

    @field_validator('name')
    @classmethod
    def _name(cls, value):
        return (value or '').strip() or None

    @field_validator('match_rules')
    @classmethod
    def _match(cls, value):
        if not value:
            raise ValueError('匹配规则不能为空：请至少填写一条有效的匹配规则(URI / Authority / Headers)')
        return value

    @field_validator('forward_type')
    @classmethod
    def _forward_type(cls, value):
        if value not in S.FORWARD_TYPES:
            raise ValueError('转发类型只能是 route 或 delegate')
        return value

    @field_validator('forward_detail')
    @classmethod
    def _forward_detail(cls, value):
        if not value:
            raise ValueError('转发详情不能为空：请完整填写服务名、命名空间和端口，或委托的 VS 名称和命名空间')
        return value

    @field_validator('timeout')
    @classmethod
    def _route_timeout(cls, value):
        return _timeout(value)

    @model_validator(mode='after')
    def _name_follows_rewrite(self):
        self.rewrite_rules = self.rewrite_rules or None
        issue = S.route_issue(self.name, self.rewrite_rules)
        if issue:
            raise ValueError(f'{issue}:规则名称用于访问日志展示,启用重写时必须填写,不重写时必须留空')
        return self

    @model_validator(mode='after')
    def _domain_blocks(self):
        for block in self.match_rules:
            if S.is_domain_block(block) and ('authority' in block or 'host' in (block.get('headers') or {})):
                raise ValueError('按集群设置域名的条件里不能再写 authority / headers.host')
        count = S.domain_block_count(self.match_rules)
        for k8s_name, lists in self.domains.items():
            if count and len(lists) != count:
                raise ValueError(f'集群 {k8s_name} 的域名和匹配条件对不上:应有 {count} 组')
        return self


def _route_payload(req: RouteRequest, standard: dict) -> dict:
    """规则 + 各集群的域名 → 入库的内容。有域名条件时,已纳入该 VS 的集群都要设置,且至少匹配一个域名。"""
    route = req.model_dump(exclude={'domains'})
    route['domains'] = {}
    if not S.domain_block_count(req.match_rules):
        return route
    bindings = standard['bindings']
    outside = [k8s_name for k8s_name in req.domains if k8s_name not in bindings]
    if outside:
        raise IstioError(f"这个 VirtualService 没有部署到集群 {'、'.join(outside)}")
    domains = {}
    for k8s_name in bindings:
        if k8s_name not in req.domains:
            raise IstioError(f'请设置集群 {k8s_name} 的域名')
        lists = _domain_lists(req.domains[k8s_name])
        if not S.render_match(req.match_rules, lists):
            raise IstioError(f'集群 {k8s_name} 一个域名都没选:这条规则在该集群会匹配所有请求')
        domains[k8s_name] = lists
    route['match_rules'], route['domains'] = S.merge_domain_blocks(req.match_rules, domains)
    return route


class ImportItem(BaseModel):
    namespace: str
    name: str
    base: str
    clusters: List[str] = Field(min_length=1)


class ImportRequest(BaseModel):
    group_id: int
    items: List[ImportItem] = Field(min_length=1, max_length=1000)


class ApplyItem(BaseModel):
    vs_id: int
    k8s_name: str


class ApplyRequest(BaseModel):
    items: List[ApplyItem] = Field(min_length=1, max_length=1000)


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------
def _agent_error(resp) -> str:
    if isinstance(resp, dict):
        return str(resp.get('error') or resp.get('message') or '未知错误')
    return str(resp)


async def fetch_live(k8s_name: str) -> List[dict]:
    """调 agent 列出集群里所有 VirtualService(完整 spec)。"""
    if _call_agent is None:
        raise IstioError('master 还没有初始化 agent 调用', 500)
    try:
        resp = await _call_agent(k8s_name, AGENT_LIST_PATH, timeout=AGENT_TIMEOUT)
    except Exception as exc:
        raise IstioError(f'集群 {k8s_name}:{exc}', 502)
    if not isinstance(resp, dict) or resp.get('success') is False or not isinstance(resp.get('data'), list):
        raise IstioError(f'集群 {k8s_name} 列出 VirtualService 失败:{_agent_error(resp)}', 502)
    items = []
    for vs in resp['data']:
        if isinstance(vs, dict) and vs.get('name') and vs.get('namespace'):
            items.append({
                'namespace': vs['namespace'],
                'name': vs['name'],
                'spec': vs.get('spec') or {},
                'resource_version': str(vs.get('resourceVersion') or ''),
            })
    return items


async def _collect_cluster(k8s_name: str, username: str) -> dict:
    try:
        items = await fetch_live(k8s_name)
        await store.replace_snapshots(k8s_name, items, username)
        return {'k8s_name': k8s_name, 'success': True, 'count': len(items)}
    except IstioError as exc:
        return {'k8s_name': k8s_name, 'success': False, 'error': exc.message}
    except Exception as exc:
        logger.exception(f'采集集群 {k8s_name} 的 VirtualService 失败: {exc}')
        return {'k8s_name': k8s_name, 'success': False, 'error': str(exc)}


# ---------------------------------------------------------------------------
# Istio 组
# ---------------------------------------------------------------------------
@handler
async def list_groups(request):
    principal = principal_of(request)
    groups = await store.list_groups()
    result = []
    for group in groups:
        visible = [c for c in group['clusters'] if principal.can_read(c['k8s_name'])]
        if principal.is_admin or visible:
            result.append({**group, 'clusters': group['clusters'] if principal.is_admin else visible})
    data = {'groups': result}
    if principal.is_admin:  # 组管理弹框:所有已接入的集群,以及各自属于哪个组
        import utils

        owner = {c['k8s_name']: g['name'] for g in groups for c in g['clusters']}
        names = sorted(set(await utils.get_k8s_names()) | set(owner))
        data['clusters'] = [{'k8s_name': name, 'group': owner.get(name, '')} for name in names]
    return _ok(data)


@handler
async def create_group(request):
    req = GroupRequest(**await _body(request))
    group_id = await store.save_group(None, req.name, req.description, [c.model_dump() for c in req.clusters])
    return _ok({'id': group_id}, msg='Istio 组已创建')


@handler
async def update_group(request):
    group_id = _int(request.query.get('id'), 'id')
    req = GroupRequest(**await _body(request))
    await store.save_group(group_id, req.name, req.description, [c.model_dump() for c in req.clusters])
    return _ok(msg='Istio 组已保存')


@handler
async def delete_group(request):
    group_id = _int(request.query.get('id'), 'id')
    if not await store.delete_group(group_id):
        raise IstioError('Istio 组不存在', 404)
    return _ok(msg='Istio 组已删除,集群里的 VirtualService 不受影响')


# ---------------------------------------------------------------------------
# 组矩阵、采集现状、对比、纳管
# ---------------------------------------------------------------------------
@handler
async def group_matrix(request):
    group = await _group(_int(request.query.get('group_id'), 'group_id'))
    visible = _visible(request, group)
    standards = await store.list_standards(group['id'])
    snapshots = await store.load_snapshots([c['k8s_name'] for c in visible])
    rows = S.build_matrix(visible, standards, snapshots)
    return _ok({'group': {**group, 'clusters': visible}, 'rows': rows})


@handler
async def collect(request):
    """刷新集群现状:并行调各集群 agent 列出 VS,整体替换各自的快照,不动组标准,互不影响。"""
    body = await _body(request)
    group = await _group(_int(body.get('group_id'), 'group_id'))
    targets = body.get('k8s_names') or [c['k8s_name'] for c in group['clusters']]
    if not isinstance(targets, list) or not all(isinstance(t, str) and t for t in targets):
        raise IstioError('k8s_names 必须是集群名数组')
    _members(group, targets)
    username = _username(request)
    results = await asyncio.gather(*(_collect_cluster(name, username) for name in targets))
    return _ok(list(results))


async def _compare_side(request, group: dict, visible: List[str], namespace: str, name: str, side: str) -> dict:
    """对比的一方:standard(组标准共用部分)、standard@集群(组标准 + 该集群 hosts 和域名)、cluster@集群(集群现状)。"""
    kind, _, k8s_name = (side or '').partition('@')
    if k8s_name and k8s_name not in visible:
        raise IstioError(f'没有集群 {k8s_name} 的访问权限', 403)
    if kind == 'standard':
        standard = await store.find_standard(group['id'], namespace, name)
        if standard is None:
            return {'side': side, 'label': '组标准', 'exists': False, 'spec': {}}
        binding = standard['bindings'].get(k8s_name) if k8s_name else None
        label = f'组标准 · {k8s_name}' if k8s_name else '组标准'
        result = {'side': side, 'label': label, 'exists': True, 'vs_id': standard['id'], 'missing_domains': []}
        if binding:
            result.update(spec=S.standard_spec(standard, binding['hosts'], k8s_name),
                          missing_domains=S.missing_domains(standard, k8s_name))
        else:
            result['spec'] = S.standard_spec(standard)
        return result
    if kind == 'cluster' and k8s_name:
        snapshots = await store.load_snapshots([k8s_name], namespace, name)
        spec = snapshots.get((k8s_name, namespace, name))
        return {'side': side, 'label': f'{k8s_name} 现状', 'exists': spec is not None, 'spec': spec or {}}
    raise IstioError('对比对象格式:standard、standard@集群 或 cluster@集群')


@handler
async def compare(request):
    query = request.query
    group = await _group(_int(query.get('group_id'), 'group_id'))
    visible = [c['k8s_name'] for c in _visible(request, group)]
    namespace, name = query.get('namespace'), query.get('name')
    if not namespace or not name:
        raise IstioError('缺少参数: namespace / name')
    left = await _compare_side(request, group, visible, namespace, name, query.get('left'))
    right = await _compare_side(request, group, visible, namespace, name, query.get('right'))
    return _ok({'left': left, 'right': right, 'diff': S.diff_specs(left['spec'], right['spec'])})


@handler
async def import_standards(request):
    """纳管:以某个集群的现状生成组标准,部署到所选集群。各集群的 hosts 和规则里的域名取各自的现状
    (集群里没有这个 VS 的,hosts 先用基准集群的;对应规则对不上的域名留空,显示「缺少域名」,补上才能下发)。"""
    req = ImportRequest(**await _body(request))
    group = await _group(req.group_id)
    names = [c['k8s_name'] for c in group['clusters']]
    snapshots = await store.load_snapshots(names)
    username = _username(request)
    results = []
    for item in req.items:
        result = {'namespace': item.namespace, 'name': item.name}
        try:
            _members(group, [item.base, *item.clusters])
            base = snapshots.get((item.base, item.namespace, item.name))
            if base is None:
                raise IstioError(f'集群 {item.base} 的现状里没有 {item.namespace}/{item.name},请先刷新现状')
            parsed = S.parse_spec(base)
            routes = parsed['routes']
            base_entries = S.split_http(base.get('http'))[0]
            for route in routes:
                route['domains'] = {}
            bindings = {}
            for k8s_name in dict.fromkeys(item.clusters):
                live = snapshots.get((k8s_name, item.namespace, item.name))
                bindings[k8s_name] = {
                    'hosts': list((live if live is not None else base).get('hosts') or []),
                    # 纳管时记下集群当时的内容:之后集群被直接改过才算「不一致」,只是和组标准不同算「待下发」
                    'applied_hash': S.spec_hash(live) if live is not None else None,
                }
                found = S.aligned_domains(base_entries, live)
                for index, route in enumerate(routes):
                    if route['domain_lists'] and index in found:
                        route['domains'][k8s_name] = found[index]
            result['vs_id'] = await store.create_standard(
                group['id'], item.namespace, item.name, parsed['vs'], routes, bindings, username)
            result['success'] = True
        except IstioError as exc:
            result.update(success=False, error=exc.message)
        results.append(result)
    return _ok(results)


@handler
async def reset_standard(request):
    """以某个集群的现状更新组标准的共用部分(规则、默认路由、gateways),各集群 hosts 不变。

    规则里的域名:该集群取自己现状里的;其他集群保持原来对应规则的域名,新出现的规则取各自现状里对应规则的。"""
    body = await _body(request)
    standard = await _standard(_int(body.get('vs_id'), 'vs_id'))
    k8s_name = body.get('k8s_name') or ''
    group = await _group(standard['group_id'])
    _members(group, [k8s_name])
    namespace, name = standard['namespace'], standard['name']
    snapshots = await store.load_snapshots([k8s_name, *standard['bindings']], namespace, name)
    live = snapshots.get((k8s_name, namespace, name))
    if live is None:
        raise IstioError(f'集群 {k8s_name} 的现状里没有这个 VirtualService,请先刷新现状')
    parsed = S.parse_spec(live)
    routes = parsed['routes']
    entries = S.split_http(live.get('http'))[0]
    old_routes = S.sort_routes(standard['routes'])
    for route in routes:
        route['domains'] = {}
    for cluster in standard['bindings']:
        kept = S.aligned_domains(entries, {'http': [S.render_route(r, cluster) for r in old_routes]})
        current = S.aligned_domains(entries, snapshots.get((cluster, namespace, name)))
        first, second = (current, kept) if cluster == k8s_name else (kept, current)
        for index, route in enumerate(routes):
            lists = first.get(index) or second.get(index)
            if route['domain_lists'] and lists:
                route['domains'][cluster] = lists
    await store.replace_standard_content(standard['id'], parsed['vs'], routes, _username(request))
    return _ok(msg=f'已按 {k8s_name} 的现状更新组标准(各集群 hosts 不变),下发后生效')


# ---------------------------------------------------------------------------
# 组标准 VS
# ---------------------------------------------------------------------------
def _cluster_rows(clusters: List[dict], standard: dict, snapshots: Dict[tuple, dict]) -> List[dict]:
    routes = S.sort_routes(standard['routes'])
    rows = []
    for cluster in clusters:
        k8s_name = cluster['k8s_name']
        binding = standard['bindings'].get(k8s_name)
        live = snapshots.get((k8s_name, standard['namespace'], standard['name']))
        row = {**cluster, 'bound': binding is not None, 'in_cluster': live is not None}
        if binding is not None:
            status, rendered = S.cluster_status(standard, binding, k8s_name, live, bool(cluster['collected_at']))
            row.update(
                hosts=binding['hosts'],
                status=status,
                applied_at=binding['applied_at'],
                applied_by=binding['applied_by'],
                missing_domains=S.missing_domains(standard, k8s_name),
                lint=S.lint_domains(rendered, f'{k8s_name}:') if status != S.INCOMPLETE else [],
            )
        else:
            row.update(hosts=list((live or {}).get('hosts') or []), status=S.UNBOUND if live else S.NONE,
                       applied_at=None, applied_by='', missing_domains=[], lint=[])
        # 集群现状里对应规则的域名:部署到该集群、补设置域名时用作默认值
        found = S.aligned_domains([S.render_route(route, k8s_name) for route in routes], live)
        row['live_domains'] = {routes[index]['id']: lists for index, lists in found.items()
                               if S.domain_block_count(routes[index]['match_rules'])}
        rows.append(row)
    return rows


@handler
async def get_vs(request):
    standard = await _standard(_int(request.query.get('vs_id'), 'vs_id'))
    group = await _group(standard['group_id'])
    visible = _visible(request, group)
    names = {c['k8s_name'] for c in visible}
    snapshots = await store.load_snapshots(names, standard['namespace'], standard['name'])
    shared = S.standard_spec(standard)
    routes = [{**route, 'issue': S.route_issue(route['name'], route['rewrite_rules']),
               'domains': {k: v for k, v in route['domains'].items() if k in names}}
              for route in S.sort_routes(standard['routes'])]
    clusters = _cluster_rows(visible, standard, snapshots)
    data = {key: standard[key] for key in (
        'id', 'group_id', 'namespace', 'name', 'gateways', 'df_forward_type', 'df_forward_detail',
        'df_forward_timeout', 'df_extra', 'extra_spec', 'updated_by', 'created_at', 'updated_at')}
    data.update(
        group={'id': group['id'], 'name': group['name']},
        clusters=clusters,
        routes=routes,
        lint=S.lint_http(shared.get('http')) + [issue for row in clusters for issue in row.pop('lint')],
    )
    return _ok(data)


@handler
async def create_vs(request):
    req = VSCreateRequest(**await _body(request))
    group = await _group(req.group_id)
    names = [c.k8s_name for c in req.clusters]
    if len(names) != len(set(names)):
        raise IstioError('集群不能重复')
    _members(group, names)
    existing = await store.load_snapshots([c['k8s_name'] for c in group['clusters']], req.namespace, req.name)
    if existing:
        clusters = '、'.join(sorted(k for k, _, _ in existing))
        raise IstioError(f'集群 {clusters} 里已经有 {req.namespace}/{req.name},请在列表里用「纳管」')
    vs = req.model_dump(include={'gateways', 'df_forward_type', 'df_forward_detail', 'df_forward_timeout'})
    bindings = {c.k8s_name: {'hosts': c.hosts} for c in req.clusters}
    vs_id = await store.create_standard(group['id'], req.namespace, req.name, vs, [], bindings, _username(request))
    return _ok({'id': vs_id}, msg='VirtualService 已创建,配好规则后到各集群下发')


@handler
async def update_vs(request):
    vs_id = _int(request.query.get('vs_id'), 'vs_id')
    req = VSFields(**await _body(request))
    await store.update_standard_fields(vs_id, req.model_dump(), _username(request))
    return _ok(msg='已保存,下发后生效')


@handler
async def delete_vs(request):
    await store.delete_standard(_int(request.query.get('vs_id'), 'vs_id'))
    return _ok(msg='组标准已删除;集群里的 VirtualService 不受影响,如需删除请到集群里处理')


@handler
async def save_vs_cluster(request):
    """VS 部署到某个集群 / 修改该集群的 hosts,可以同时设置该集群在各规则里的域名。"""
    req = BindingRequest(**await _body(request))
    standard = await _standard(req.vs_id)
    _members(await _group(standard['group_id']), [req.k8s_name])
    domains = None
    if req.domains is not None:
        routes = S.sort_routes(standard['routes'])
        by_id = {route['id']: (index + 1, route) for index, route in enumerate(routes)}
        domains = {}
        for route_id, lists in req.domains.items():
            if route_id not in by_id:
                raise IstioError('规则不存在(可能刚被删除),请刷新后重试')
            position, route = by_id[route_id]
            count = S.domain_block_count(route['match_rules'])
            if not count:
                continue
            if len(lists) != count:
                raise IstioError(f'第 {position} 条规则的域名和匹配条件对不上:应有 {count} 组')
            lists = _domain_lists(lists)
            if not S.render_match(route['match_rules'], lists):
                raise IstioError(f'第 {position} 条规则一个域名都没选:这条规则在该集群会匹配所有请求')
            domains[route_id] = lists
    await store.upsert_binding(req.vs_id, req.k8s_name, req.hosts, domains)
    return _ok(msg='已保存,下发后生效')


@handler
async def remove_vs_cluster(request):
    vs_id = _int(request.query.get('vs_id'), 'vs_id')
    k8s_name = request.query.get('k8s_name') or ''
    await store.delete_binding(vs_id, k8s_name)
    return _ok(msg=f'已不再部署到 {k8s_name};集群里的 VirtualService 不受影响,如需删除请到集群里处理')


# ---------------------------------------------------------------------------
# 组标准的 http 规则
# ---------------------------------------------------------------------------
@handler
async def create_route(request):
    vs_id = _int(request.query.get('vs_id'), 'vs_id')
    req = RouteRequest(**await _body(request))
    route = _route_payload(req, await _standard(vs_id))
    route_id = await store.create_route(vs_id, route, _username(request))
    return _ok({'id': route_id}, msg='规则已添加,下发后生效')


@handler
async def update_route(request):
    route_id = _int(request.query.get('route_id'), 'route_id')
    req = RouteRequest(**await _body(request))
    vs_id = await store.route_vs_id(route_id)
    if vs_id is None:
        raise IstioError('规则不存在', 404)
    route = _route_payload(req, await _standard(vs_id))
    await store.update_route(route_id, route, _username(request))
    return _ok(msg='规则已保存,下发后生效')


@handler
async def delete_route(request):
    await store.delete_route(_int(request.query.get('route_id'), 'route_id'), _username(request))
    return _ok(msg='规则已删除,下发后生效')


@handler
async def reorder_routes(request):
    await store.reorder_routes(_int(request.query.get('vs_id'), 'vs_id'), _username(request))
    return _ok(msg='优先级已重新整理')


# ---------------------------------------------------------------------------
# 下发
# ---------------------------------------------------------------------------
async def _prepare(items: List[ApplyItem]) -> List[dict]:
    """每一项都必须是组标准已纳入的集群;生成要下发的 spec。规则的域名没设置全的,error 说明缺哪些。"""
    standards: Dict[int, dict] = {}
    prepared = []
    for item in items:
        if item.vs_id not in standards:
            standards[item.vs_id] = await _standard(item.vs_id)
        standard = standards[item.vs_id]
        binding = standard['bindings'].get(item.k8s_name)
        if binding is None:
            raise IstioError(f"{standard['namespace']}/{standard['name']} 没有部署到集群 {item.k8s_name}")
        rendered = S.standard_spec(standard, binding['hosts'], item.k8s_name)
        missing = S.missing_domains(standard, item.k8s_name)
        prepared.append({
            'vs_id': standard['id'], 'k8s_name': item.k8s_name,
            'namespace': standard['namespace'], 'name': standard['name'],
            'spec': rendered, 'hash': S.spec_hash(rendered),
            'error': f"还没设置 {item.k8s_name} 的域名:{'、'.join(missing)}" if missing else None,
        })
    return prepared


def _by_cluster(prepared: List[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for item in prepared:
        grouped.setdefault(item['k8s_name'], []).append(item)
    return grouped


@handler
async def apply_preview(request):
    """下发前预览:从集群取最新现状(顺便刷新快照),和要下发的内容逐项对比。"""
    req = ApplyRequest(**await _body(request))
    prepared = await _prepare(req.items)
    username = _username(request)

    async def load(k8s_name):
        try:
            items = await fetch_live(k8s_name)
        except IstioError as exc:
            return k8s_name, None, exc.message
        await store.replace_snapshots(k8s_name, items, username)
        return k8s_name, {(i['namespace'], i['name']): i['spec'] for i in items}, None

    loaded = await asyncio.gather(*(load(name) for name in _by_cluster(prepared)))
    live_by_cluster = {name: (live, error) for name, live, error in loaded}
    result = []
    for item in prepared:
        live, error = live_by_cluster[item['k8s_name']]
        error = item['error'] or error
        current = (live or {}).get((item['namespace'], item['name']))
        result.append({
            'vs_id': item['vs_id'], 'k8s_name': item['k8s_name'],
            'namespace': item['namespace'], 'name': item['name'],
            'error': error, 'exists': current is not None,
            'live': current, 'rendered': item['spec'],
            'same': error is None and current is not None and S.spec_hash(current) == item['hash'],
            # 集群现状(左)→ 将要下发(右):right_only 是要新增的规则,left_only 是要删掉的
            'diff': S.diff_specs(current, item['spec']) if current is not None and error is None else None,
        })
    return _ok(result)


async def _apply_one(item: dict, username: str, ip: str) -> dict:
    body = S.manifest(item['namespace'], item['name'], item['spec'])
    try:
        resp = await _call_agent(item['k8s_name'], AGENT_APPLY_PATH, method='POST', body=body, timeout=AGENT_TIMEOUT)
    except Exception as exc:
        resp = {'success': False, 'error': str(exc)}
    success = isinstance(resp, dict) and resp.get('success') is True
    error = None if success else _agent_error(resp)
    if success:
        await store.mark_applied(item['vs_id'], item['k8s_name'], item['hash'], username)
    audit.record_bg(username, 'istio.apply', env=item['k8s_name'], target=f"{item['namespace']}/{item['name']}",
                    ip=ip, success=success, detail={'vs_id': item['vs_id'], **({'error': error} if error else {})})
    return {'vs_id': item['vs_id'], 'k8s_name': item['k8s_name'], 'namespace': item['namespace'],
            'name': item['name'], 'success': success, 'error': error}


@handler
async def apply(request):
    """按组标准 + 各集群 hosts 下发(agent 用 Server-Side Apply)。同一集群依次下发,不同集群并行;
    下发完重新采集该集群现状,状态以集群里的实际内容为准。"""
    req = ApplyRequest(**await _body(request))
    prepared = await _prepare(req.items)
    blocked = [f"{i['namespace']}/{i['name']} → {i['error']}" for i in prepared if i['error']]
    if blocked:
        raise IstioError('；'.join(blocked))
    username, ip = _username(request), client_ip(request)

    async def run(k8s_name, items):
        results = [await _apply_one(item, username, ip) for item in items]
        try:
            await store.replace_snapshots(k8s_name, await fetch_live(k8s_name), username)
        except Exception as exc:
            logger.warning(f'下发后刷新集群 {k8s_name} 的现状失败: {exc}')
        return results

    grouped = await asyncio.gather(*(run(name, items) for name, items in _by_cluster(prepared).items()))
    return _ok([result for results in grouped for result in results])


def register_routes(app, call_agent_api):
    global _call_agent
    _call_agent = call_agent_api
    router = app.router
    router.add_get('/api/istio/groups', list_groups)
    router.add_post('/api/istio/groups', create_group)
    router.add_put('/api/istio/groups', update_group)
    router.add_delete('/api/istio/groups', delete_group)
    router.add_get('/api/istio/matrix', group_matrix)
    router.add_post('/api/istio/collect', collect)
    router.add_get('/api/istio/compare', compare)
    router.add_post('/api/istio/import', import_standards)
    router.add_post('/api/istio/vs/reset', reset_standard)
    router.add_get('/api/istio/vs', get_vs)
    router.add_post('/api/istio/vs', create_vs)
    router.add_put('/api/istio/vs', update_vs)
    router.add_delete('/api/istio/vs', delete_vs)
    router.add_put('/api/istio/vs/cluster', save_vs_cluster)
    router.add_delete('/api/istio/vs/cluster', remove_vs_cluster)
    router.add_post('/api/istio/httproute', create_route)
    router.add_put('/api/istio/httproute', update_route)
    router.add_delete('/api/istio/httproute', delete_route)
    router.add_post('/api/istio/httproute/reorder', reorder_routes)
    router.add_post('/api/istio/apply/preview', apply_preview)
    router.add_post('/api/istio/apply', apply)
