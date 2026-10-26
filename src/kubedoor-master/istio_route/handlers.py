"""Istio VirtualService 管理接口(/api/istio/*),设计见 README.md。

权限(auth/policy.py 登记):查看按集群读权限,看不到的集群不返回;所有修改只允许管理员。
集群里的 VS 只经这里读写:master 调 agent 的 /api/agent/istio/vs(列出)、/api/agent/istio/vs/detail(查看单个)、
/api/agent/istio/vs/apply(下发)和 /api/agent/istio/vs/delete(删除),下发内容由 master 按组标准生成,
不接受前端传来的 spec;浏览器不能经通用转发直接调这些 agent 接口。
改集群里的 VS 之前(下发、回退、删除)都先备份它当时的内容,用于回退(istio_vs_backup)。
"""

import asyncio
import functools
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple, Union

from aiohttp import web
from loguru import logger
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from auth import audit, client_ip, principal_of
from . import spec as S
from . import store
from .store import IstioError

AGENT_LIST_PATH = '/api/agent/istio/vs'
AGENT_DETAIL_PATH = '/api/agent/istio/vs/detail'
AGENT_APPLY_PATH = '/api/agent/istio/vs/apply'
AGENT_DELETE_PATH = '/api/agent/istio/vs/delete'
AGENT_TIMEOUT = 60
BEIJING = timezone(timedelta(hours=8))

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


def _beijing(value: datetime) -> str:
    return value.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')


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
# 组的 VS 范围与跨组冲突
# ---------------------------------------------------------------------------
def _safe_scope(include, ignore, overrides: Dict[tuple, dict]) -> S.VSScope:
    """库里的规则保存时都校验过;万一有编译不了的(如换了 Python 版本),跳过那一条,不影响页面。"""
    def usable(items):
        kept = []
        for item in items or []:
            try:
                S.compile_pattern(item)
                kept.append(item)
            except (re.error, KeyError, TypeError) as exc:
                logger.warning(f'跳过无效的 Istio 组规则 {item}: {exc}')
        return kept

    return S.VSScope(usable(include), usable(ignore), {key: row['action'] for key, row in overrides.items()})


class GroupView:
    """一个组(或组管理里正在编辑的组)的 VS 范围,以及和它共用集群的其他组 —— 判断忽略和冲突用。"""

    def __init__(self, group_id: Optional[int], clusters: List[str], scope: S.VSScope,
                 overrides: Dict[tuple, dict], standards: set, others: List[dict], present: Dict[str, set]):
        self.group_id = group_id
        self.clusters = clusters
        self.scope = scope
        self.overrides = overrides      # 本组的手动决定:{(namespace, name): {action, created_by, created_at}}
        self.standards = standards      # 本组有组标准的 VS(含被忽略的)
        self.others = others
        self.present = present          # {k8s_name: {(namespace, name)}},本组和其他组集群的现状

    def classify(self, key: tuple) -> dict:
        return self.scope.classify(*key)

    def conflicts(self, keys: Iterable[tuple]) -> Dict[tuple, List[dict]]:
        return S.find_conflicts(self.group_id, self.clusters, keys, self.others, self.present)


async def _group_view(group_id: Optional[int], clusters: List[str], include, ignore) -> GroupView:
    groups = await store.list_groups()
    mine = set(clusters)
    related = [g for g in groups if g['id'] != group_id and mine & {c['k8s_name'] for c in g['clusters']}]
    ids = [g['id'] for g in related] + ([group_id] if group_id is not None else [])
    overrides = await store.load_overrides(ids)
    standards = await store.standard_keys(ids)
    present = await store.snapshot_keys(mine | {c['k8s_name'] for g in related for c in g['clusters']})
    others = [{
        'id': g['id'], 'name': g['name'], 'clusters': [c['k8s_name'] for c in g['clusters']],
        'scope': _safe_scope(g['include_patterns'], g['ignore_patterns'], overrides[g['id']]),
        'standards': standards[g['id']],
    } for g in related]
    own = overrides.get(group_id, {}) if group_id is not None else {}
    return GroupView(group_id, list(clusters), _safe_scope(include, ignore, own), own,
                     standards.get(group_id, set()) if group_id is not None else set(), others, present)


async def _view_of(group: dict) -> GroupView:
    return await _group_view(group['id'], [c['k8s_name'] for c in group['clusters']],
                             group['include_patterns'], group['ignore_patterns'])


def _scope_text(result: dict) -> str:
    """被排除的原因,给提示用。"""
    if result['reason'] == S.SCOPE_MANUAL:
        return '已在本组手动忽略'
    if result['reason'] == S.SCOPE_IGNORE_RULE:
        return f"匹配本组的忽略规则「{result['pattern']}」"
    if result['reason'] == S.SCOPE_NOT_INCLUDED:
        return '不在本组的包含规则里'
    return ''


def _conflict_text(conflicts: List[dict]) -> str:
    return '、'.join(f"组 {c['group']}(共用集群 {'、'.join(c['clusters'])})" for c in conflicts)


def _conflict_error(key: tuple, conflicts: List[dict]) -> str:
    return (f'{key[0]}/{key[1]} 也在{_conflict_text(conflicts)}里;同一个 VS 只能属于一个组,'
            '请先在不该管它的组里忽略它')


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


class PatternItem(BaseModel):
    """组的包含 / 忽略规则:glob 通配符(* 任意多个字符,? 一个字符)或 regex 正则(全匹配)。
    规则里有 / 时匹配「命名空间/名称」,否则只匹配名称。"""

    type: str = 'glob'
    pattern: str

    @field_validator('type')
    @classmethod
    def _type(cls, value):
        if value not in S.PATTERN_TYPES:
            raise ValueError('规则类型只能是 glob(通配符)或 regex(正则)')
        return value

    @field_validator('pattern')
    @classmethod
    def _pattern(cls, value):
        value = (value or '').strip()
        if not value:
            raise ValueError('规则不能为空')
        if len(value) > 300:
            raise ValueError('规则太长(最多 300 个字符)')
        return value


def _check_patterns(items: List[PatternItem], what: str) -> List[PatternItem]:
    """正则要能编译;重复的去掉。"""
    result, seen = [], set()
    for index, item in enumerate(items):
        try:
            S.compile_pattern(item.model_dump())
        except re.error as exc:
            raise ValueError(f'{what}第 {index + 1} 条「{item.pattern}」正则写法有误:{exc}')
        if (item.type, item.pattern) not in seen:
            seen.add((item.type, item.pattern))
            result.append(item)
    return result


class ScopeRules(BaseModel):
    """组的 VS 范围:包含规则为空表示全部;忽略规则优先于包含规则。"""

    include_patterns: List[PatternItem] = Field(default=[], max_length=200)
    ignore_patterns: List[PatternItem] = Field(default=[], max_length=200)

    @model_validator(mode='after')
    def _patterns(self):
        self.include_patterns = _check_patterns(self.include_patterns, '包含规则')
        self.ignore_patterns = _check_patterns(self.ignore_patterns, '忽略规则')
        return self

    def rules(self):
        return ([item.model_dump() for item in self.include_patterns],
                [item.model_dump() for item in self.ignore_patterns])


class GroupRequest(ScopeRules):
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


class VSKey(BaseModel):
    namespace: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)


class ScopeChangeRequest(BaseModel):
    """在组里忽略 / 取消忽略这些 VS。"""

    group_id: int
    items: List[VSKey] = Field(min_length=1, max_length=1000)


class LiveDeleteRequest(VSKey):
    """删除集群里的 VirtualService。"""

    group_id: int
    k8s_name: str = Field(min_length=1, max_length=255)


class RollbackRequest(LiveDeleteRequest):
    """把集群里的 VirtualService 回退到某一份备份。"""

    backup_id: int


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
    # 名称被本组的包含 / 忽略规则排除时,仍然创建并单独纳入本组
    force_include: bool = False

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


class SyncPick(BaseModel):
    """对比里的一行规则:组标准里的第几条、集群现状里的第几条(从 0 开始,没有就是空)。"""

    standard_index: Optional[int] = Field(default=None, ge=0)
    live_index: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode='after')
    def _one_side(self):
        if self.standard_index is None and self.live_index is None:
            raise ValueError('要同步的规则两边都是空的')
        return self


class SyncRequest(VSKey):
    """从集群现状整条同步规则(以及 gateways / 默认路由 / 其他字段)到组标准。"""

    group_id: int
    # 对比里组标准一侧:standard 或 standard@集群,规则按它和现状对齐
    standard: str = 'standard'
    k8s_name: str = Field(min_length=1, max_length=255)
    rules: List[SyncPick] = Field(default_factory=list, max_length=2000)
    gateways: bool = False
    default: bool = False
    extra: bool = False
    # 打开对比时两边内容的哈希(对比接口的 version):之后变了就不能按当时的对齐结果同步
    standard_version: str
    live_version: str

    @model_validator(mode='after')
    def _check(self):
        if self.standard != 'standard' and not self.standard.startswith('standard@'):
            raise ValueError('standard 只能是 standard 或 standard@集群')
        if not (self.rules or self.gateways or self.default or self.extra):
            raise ValueError('没有要同步的内容')
        return self


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


async def _agent_write(k8s_name: str, path: str, method: str, query=None, body=None) -> Tuple[Optional[bool], dict]:
    """调 agent 改集群里的 VS:(结果, 回包)。结果 True 成功;False 是 agent 明确回了失败,集群没变;
    None 是不清楚(等待超时、连接断开),可能已经生效。"""
    try:
        resp = await _call_agent(k8s_name, path, query=query, method=method, body=body, timeout=AGENT_TIMEOUT)
    except Exception as exc:
        return None, {'success': False, 'error': str(exc)}
    return isinstance(resp, dict) and resp.get('success') is True, resp


# ---------------------------------------------------------------------------
# 回退用的备份:改集群里的 VS 之前(下发、回退、删除)先存下它当时的 spec,设计见 README「下发前备份与回退」
# ---------------------------------------------------------------------------
BACKUP_ACTION_TEXT = {'apply': '下发', 'rollback': '回退', 'delete': '删除'}


def _same_spec(a: Optional[dict], b: Optional[dict]) -> bool:
    """两个 spec 是否一样;None 表示集群里没有。"""
    if a is None or b is None:
        return a is None and b is None
    return S.spec_hash(a) == S.spec_hash(b)


async def _backup(k8s_name: str, namespace: str, name: str, live: Optional[dict], action: str, username: str) -> int:
    """备份集群里这个 VS 当时的内容;live 是 fetch_live 取到的那一项,None 表示集群里没有。"""
    return await store.add_backup(k8s_name, namespace, name, live['spec'] if live else None,
                                  live['resource_version'] if live else '', action, username)


async def _settle_backup(backup_id: int, key: tuple, ok: Optional[bool], keep_id: Optional[int] = None) -> None:
    """改完集群后处理刚做的备份:agent 明确回了失败(集群没变)就删掉它,否则只留最近几份(见 store.prune_backups)。
    这一步出错不影响操作结果,下次写入时会再清理。key 是 (集群, 命名空间, 名称)。"""
    try:
        if ok is False:
            await store.drop_backup(backup_id)
        else:
            await store.prune_backups(*key, keep_id=keep_id)
    except Exception as exc:
        logger.warning(f'整理 {key} 的备份失败: {exc}')


async def _collect_cluster(k8s_name: str, username: str) -> dict:
    try:
        items = await fetch_live(k8s_name)
        await store.replace_snapshots(k8s_name, items, username)
        return {'k8s_name': k8s_name, 'success': True, 'count': len(items),
                'keys': [(item['namespace'], item['name']) for item in items]}
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
    if principal.is_admin:  # 组管理弹框:所有已接入的集群,以及各自已在哪些组里(一个集群可以属于多个组)
        import utils

        owners: Dict[str, List[str]] = {}
        for g in groups:
            for c in g['clusters']:
                owners.setdefault(c['k8s_name'], []).append(g['name'])
        names = sorted(set(await utils.get_k8s_names()) | set(owners))
        data['clusters'] = [{'k8s_name': name, 'groups': owners.get(name, [])} for name in names]
    return _ok(data)


@handler
async def create_group(request):
    req = GroupRequest(**await _body(request))
    group_id = await store.save_group(None, req.name, req.description, [c.model_dump() for c in req.clusters],
                                      *req.rules())
    return _ok({'id': group_id}, msg='Istio 组已创建')


@handler
async def update_group(request):
    group_id = _int(request.query.get('id'), 'id')
    req = GroupRequest(**await _body(request))
    await store.save_group(group_id, req.name, req.description, [c.model_dump() for c in req.clusters],
                           *req.rules())
    return _ok(msg='Istio 组已保存')


@handler
async def delete_group(request):
    group_id = _int(request.query.get('id'), 'id')
    if not await store.delete_group(group_id):
        raise IstioError('Istio 组不存在', 404)
    return _ok(msg='Istio 组已删除,集群里的 VirtualService 不受影响')


@handler
async def preview_group(request):
    """组管理里预览包含 / 忽略规则的效果:按所选集群已采集的现状,哪些 VS 归本组、哪些被忽略、哪些和其他组冲突。"""
    query = request.query
    group_id = _int(query.get('group_id'), 'group_id') if query.get('group_id') else None
    try:
        rules = ScopeRules(include_patterns=json.loads(query.get('include') or '[]'),
                           ignore_patterns=json.loads(query.get('ignore') or '[]'))
    except ValueError as exc:
        if isinstance(exc, ValidationError):
            raise
        raise IstioError('include / ignore 必须是 JSON 数组')
    principal = principal_of(request)
    clusters = [c for c in dict.fromkeys((query.get('clusters') or '').split(',')) if c and principal.can_read(c)]
    view = await _group_view(group_id, clusters, *rules.rules())
    where: Dict[tuple, List[str]] = {}
    for k8s_name in clusters:
        for key in view.present.get(k8s_name, ()):
            where.setdefault(key, []).append(k8s_name)
    keys = set(where) | view.standards
    classes = {key: view.classify(key) for key in keys}
    shown = [key for key in keys if classes[key]['visible']]
    conflicts = view.conflicts(shown)
    collected = await store.collected_clusters(clusters)
    items = [{'namespace': key[0], 'name': key[1], **classes[key], 'clusters': where.get(key, []),
              'managed': key in view.standards, 'conflicts': conflicts.get(key, [])} for key in sorted(keys)]
    return _ok({
        'total': len(keys), 'visible': len(shown), 'ignored': len(keys) - len(shown), 'conflict': len(conflicts),
        'uncollected': [c for c in clusters if c not in collected], 'items': items,
    })


# ---------------------------------------------------------------------------
# 组矩阵、采集现状、对比、纳管
# ---------------------------------------------------------------------------
@handler
async def group_matrix(request):
    """组矩阵:只列本组范围内的 VS(按组规则和手动忽略 / 单独纳入),同时给出被忽略的列表和统计。"""
    group = await _group(_int(request.query.get('group_id'), 'group_id'))
    visible = _visible(request, group)
    names = [c['k8s_name'] for c in visible]
    view = await _view_of(group)
    standards = {(s['namespace'], s['name']): s for s in await store.list_standards(group['id'])}
    snapshots = await store.load_snapshots(names)
    order = {name: index for index, name in enumerate(names)}
    where: Dict[tuple, List[str]] = {}  # VS 在哪些集群的现状里(按列的顺序)
    for (k8s_name, namespace, name) in sorted(snapshots, key=lambda k: order[k[0]]):
        where.setdefault((namespace, name), []).append(k8s_name)
    keys = set(standards) | set(where)
    classes = {key: view.classify(key) for key in keys}
    shown = {key for key in keys if classes[key]['visible']}
    # 每个 VS 按集群渲染、算哈希、展平搜索内容,VS 多时要算上一秒,放到线程里免得卡住 event loop
    rows = await asyncio.to_thread(S.build_matrix, visible, [s for key, s in standards.items() if key in shown],
                                   {k: v for k, v in snapshots.items() if (k[1], k[2]) in shown})
    conflicts = view.conflicts(shown)
    for row in rows:
        key = (row['namespace'], row['name'])
        row['conflicts'] = conflicts.get(key, [])
        row['included'] = classes[key]['reason'] == S.SCOPE_INCLUDED

    ignored = []
    for key in sorted(keys - shown):
        override = view.overrides.get(key) if classes[key]['reason'] == S.SCOPE_MANUAL else None
        ignored.append({
            'namespace': key[0], 'name': key[1], 'reason': classes[key]['reason'], 'pattern': classes[key]['pattern'],
            'vs_id': standards[key]['id'] if key in standards else None, 'clusters': where.get(key, []),
            'by': override['created_by'] if override else '', 'at': override['created_at'] if override else None,
            'stale': False,
        })
    # 手动忽略过、但现状和组标准里都已经没有的(集群里删掉了):也列出来,可以取消
    for key, override in sorted(view.overrides.items()):
        if override['action'] == S.OVERRIDE_IGNORE and key not in keys:
            ignored.append({'namespace': key[0], 'name': key[1], 'reason': S.SCOPE_MANUAL, 'pattern': None,
                            'vs_id': None, 'clusters': [], 'by': override['created_by'],
                            'at': override['created_at'], 'stale': True})
    manual = sum(1 for key in keys - shown if classes[key]['reason'] == S.SCOPE_MANUAL)
    stats = {'total': len(keys), 'visible': len(shown), 'ignored': len(keys) - len(shown), 'manual': manual,
             'by_rule': len(keys) - len(shown) - manual, 'conflict': len(conflicts)}
    return _ok({'group': {**group, 'clusters': visible}, 'rows': rows, 'ignored': ignored, 'stats': stats})


@handler
async def collect(request):
    """刷新集群现状:并行调各集群 agent 列出 VS,整体替换各自的快照(各组共用),不动组标准,互不影响。
    返回每个集群的 VS 总数,以及按本组规则归本组的和被忽略的个数。"""
    body = await _body(request)
    group = await _group(_int(body.get('group_id'), 'group_id'))
    targets = body.get('k8s_names') or [c['k8s_name'] for c in group['clusters']]
    if not isinstance(targets, list) or not all(isinstance(t, str) and t for t in targets):
        raise IstioError('k8s_names 必须是集群名数组')
    _members(group, targets)
    username = _username(request)
    results = await asyncio.gather(*(_collect_cluster(name, username) for name in targets))
    overrides = (await store.load_overrides([group['id']]))[group['id']]
    scope = _safe_scope(group['include_patterns'], group['ignore_patterns'], overrides)
    for result in results:
        keys = result.pop('keys', None)
        if keys is not None:
            result['group_count'] = sum(1 for key in keys if scope.visible(*key))
            result['ignored_count'] = len(keys) - result['group_count']
    return _ok(list(results))


def _standard_view(standard: dict, k8s_name: str) -> Tuple[dict, Optional[str]]:
    """对比里组标准一侧显示的内容:给了已纳入的集群用该集群的 hosts 和域名,否则是共用部分(域名显示占位)。
    返回 (spec, 用了哪个集群的域名);从集群现状同步到组标准时按同样的内容对齐规则。"""
    binding = standard['bindings'].get(k8s_name) if k8s_name else None
    if binding:
        return S.standard_spec(standard, binding['hosts'], k8s_name), k8s_name
    return S.standard_spec(standard), None


async def _compare_side(request, group: dict, visible: List[str], namespace: str, name: str, side: str) -> dict:
    """对比的一方:standard(组标准共用部分)、standard@集群(组标准 + 该集群 hosts 和域名)、cluster@集群(集群现状)。
    version 是内容的哈希,从集群现状同步到组标准时用来确认两边没变。"""
    kind, _, k8s_name = (side or '').partition('@')
    if k8s_name and k8s_name not in visible:
        raise IstioError(f'没有集群 {k8s_name} 的访问权限', 403)
    if kind == 'standard':
        standard = await store.find_standard(group['id'], namespace, name)
        if standard is None:
            return {'side': side, 'label': '组标准', 'exists': False, 'spec': {}, 'version': None}
        spec, rendered_for = _standard_view(standard, k8s_name)
        return {'side': side, 'label': f'组标准 · {k8s_name}' if k8s_name else '组标准', 'exists': True,
                'vs_id': standard['id'], 'spec': spec, 'version': S.spec_hash(spec),
                'missing_domains': S.missing_domains(standard, rendered_for) if rendered_for else []}
    if kind == 'cluster' and k8s_name:
        snapshots = await store.load_snapshots([k8s_name], namespace, name)
        spec = snapshots.get((k8s_name, namespace, name))
        return {'side': side, 'label': f'{k8s_name} 现状', 'exists': spec is not None, 'spec': spec or {},
                'version': S.spec_hash(spec) if spec is not None else None}
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
    view = await _view_of(group)
    username = _username(request)
    results = []
    for item in req.items:
        result = {'namespace': item.namespace, 'name': item.name}
        try:
            _members(group, [item.base, *item.clusters])
            key = (item.namespace, item.name)
            scope = view.classify(key)
            if not scope['visible']:
                raise IstioError(f'{_scope_text(scope)},不能纳管;如需由本组管理,先在「查看忽略」里取消忽略')
            conflicts = view.conflicts([key]).get(key)
            if conflicts:
                raise IstioError(_conflict_error(key, conflicts))
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


async def _sync_draft(request) -> Tuple[SyncRequest, dict, Optional[str], dict]:
    """从集群现状同步到组标准:按对比时的对齐结果算出新的组标准。返回 (请求, 组标准, 对齐用了哪个集群的域名, 新组标准)。
    打开对比之后组标准或现状变了(对齐结果可能不一样)就拒绝,让用户刷新后重选。"""
    req = SyncRequest(**await _body(request))
    group = await _group(req.group_id)
    render_k8s = req.standard.partition('@')[2]
    _members(group, [req.k8s_name, *([render_k8s] if render_k8s else [])])
    standard = await store.find_standard(group['id'], req.namespace, req.name)
    if standard is None:
        raise IstioError(f'组里没有 {req.namespace}/{req.name} 的组标准', 404)
    snapshots = await store.load_snapshots([req.k8s_name, *standard['bindings']], req.namespace, req.name)
    live = snapshots.get((req.k8s_name, req.namespace, req.name))
    if live is None:
        raise IstioError(f'集群 {req.k8s_name} 的现状里没有这个 VirtualService,请先刷新现状', 404)
    shown, rendered_for = _standard_view(standard, render_k8s)
    if S.spec_hash(shown) != req.standard_version or S.spec_hash(live) != req.live_version:
        raise IstioError(f'打开对比之后组标准或 {req.k8s_name} 的现状变了,请刷新对比后重新选择要同步的内容', 409)
    try:
        vs, routes, summary = S.sync_standard(
            standard, rendered_for, req.k8s_name, live,
            {c: snapshots.get((c, req.namespace, req.name)) for c in standard['bindings']},
            [(pick.standard_index, pick.live_index) for pick in req.rules],
            gateways=req.gateways, default=req.default, extra=req.extra)
    except ValueError as exc:
        raise IstioError(f'{exc},请刷新对比后重新选择', 409)
    return req, standard, rendered_for, {**standard, **vs, 'routes': routes, 'summary': summary}


@handler
async def sync_preview(request):
    """同步到组标准的预览(不保存):保存后的组标准(和对比里组标准一侧一样显示)、改了什么、哪些集群会缺少域名。"""
    _, standard, rendered_for, draft = await _sync_draft(request)
    binding = standard['bindings'].get(rendered_for) if rendered_for else None
    missing = [{'k8s_name': c, 'rules': rules} for c in standard['bindings'] if (rules := S.missing_domains(draft, c))]
    return _ok({'spec': S.standard_spec(draft, binding['hosts'] if binding else None, rendered_for),
                'summary': draft['summary'], 'missing_domains': missing})


@handler
async def sync_to_standard(request):
    """从集群现状整条同步规则(以及 gateways / 默认路由 / 其他字段)到组标准并保存;下发后才在各集群生效。"""
    req, standard, _, draft = await _sync_draft(request)
    summary = draft['summary']
    parts = [f'{text} {summary[key]} 条规则' for key, text in
             (('added', '加入'), ('replaced', '替换'), ('deleted', '删除'), ('moved', '调整顺序')) if summary[key]]
    parts += [text for key, text in (('gateways', 'gateways'), ('default', '默认路由'), ('extra', '其他字段'))
              if summary[key]]
    if not parts:
        return _ok(msg='选中的内容和组标准一样,没有要保存的修改')
    await store.replace_standard_content(standard['id'], draft, draft['routes'], _username(request))
    return _ok(msg=f"已从 {req.k8s_name} 的现状同步到组标准:{'、'.join(parts)},下发后生效")


# ---------------------------------------------------------------------------
# 组标准 VS
# ---------------------------------------------------------------------------
def _cluster_rows(clusters: List[dict], standard: dict, snapshots: Dict[tuple, dict],
                  backups: Dict[str, dict]) -> List[dict]:
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
        # 回退用的备份有几份;最近一次下发之后又回退过、集群里也不是下发的内容了,就说明是谁、什么时候回退的
        last = backups.get(k8s_name)
        row['backup_count'] = last['count'] if last else 0
        rolled_back = bool(last) and last['action'] == 'rollback' and (
            row['applied_at'] is None or last['created_at'] > row['applied_at'])
        if rolled_back and binding is not None and live is not None:
            rolled_back = S.spec_hash(live) != binding['applied_hash']
        row['rolled_back'] = {'by': last['created_by'], 'at': last['created_at']} if rolled_back else None
        rows.append(row)
    return rows


@handler
async def get_vs(request):
    standard = await _standard(_int(request.query.get('vs_id'), 'vs_id'))
    group = await _group(standard['group_id'])
    visible = _visible(request, group)
    names = {c['k8s_name'] for c in visible}
    snapshots = await store.load_snapshots(names, standard['namespace'], standard['name'])
    backups = await store.backup_stats(names, standard['namespace'], standard['name'])
    shared = S.standard_spec(standard)
    routes = [{**route, 'issue': S.route_issue(route['name'], route['rewrite_rules']),
               'domains': {k: v for k, v in route['domains'].items() if k in names}}
              for route in S.sort_routes(standard['routes'])]
    clusters = _cluster_rows(visible, standard, snapshots, backups)
    view = await _view_of(group)
    vs_key = (standard['namespace'], standard['name'])
    data = {key: standard[key] for key in (
        'id', 'group_id', 'namespace', 'name', 'gateways', 'df_forward_type', 'df_forward_detail',
        'df_forward_timeout', 'df_extra', 'extra_spec', 'updated_by', 'created_at', 'updated_at')}
    data.update(
        group={'id': group['id'], 'name': group['name']},
        clusters=clusters,
        routes=routes,
        lint=S.lint_http(shared.get('http')) + [issue for row in clusters for issue in row.pop('lint')],
        scope=view.classify(vs_key),
        conflicts=view.conflicts([vs_key]).get(vs_key, []),
    )
    return _ok(data)


async def _check_new_vs(view: GroupView, namespace: str, name: str) -> dict:
    """新建前检查:本组是否已有组标准(可能被忽略了)、集群里是否已有同名的(要用纳管)、
    其他组里是否已有、名称是否被本组的规则 / 手动忽略排除。"""
    key = (namespace, name)
    return {
        'scope': view.classify(key),
        'standard': key in view.standards,
        'in_clusters': sorted(k8s for k8s in view.clusters if key in view.present.get(k8s, ())),
        'conflicts': view.conflicts([key]).get(key, []),
    }


@handler
async def check_vs(request):
    """新建 VS 前的检查(见 _check_new_vs),前端据此提示;名称被本组排除时可以确认后单独纳入。"""
    query = request.query
    group = await _group(_int(query.get('group_id'), 'group_id'))
    _visible(request, group)
    namespace, name = (query.get('namespace') or '').strip(), (query.get('name') or '').strip()
    if not namespace or not name:
        raise IstioError('缺少参数: namespace / name')
    return _ok(await _check_new_vs(await _view_of(group), namespace, name))


@handler
async def create_vs(request):
    req = VSCreateRequest(**await _body(request))
    group = await _group(req.group_id)
    names = [c.k8s_name for c in req.clusters]
    if len(names) != len(set(names)):
        raise IstioError('集群不能重复')
    _members(group, names)
    view = await _view_of(group)
    target, key = f'{req.namespace}/{req.name}', (req.namespace, req.name)
    check = await _check_new_vs(view, req.namespace, req.name)
    excluded = not check['scope']['visible']
    hint = ',它在本组被忽略了:先在「查看忽略」里取消忽略' if excluded else ''
    if check['standard']:
        raise IstioError(f'本组已经有 {target} 的组标准{hint}')
    if check['in_clusters']:
        raise IstioError(f"集群 {'、'.join(check['in_clusters'])} 里已经有 {target},请在列表里用「纳管」{hint}")
    if check['conflicts']:
        raise IstioError(f"{_conflict_text(check['conflicts'])}里已经有 {target},同一个 VS 只能属于一个组")
    if excluded and not req.force_include:
        raise IstioError(f"{target} {_scope_text(check['scope'])},创建后会被本组忽略;确认要创建的话请选择「单独纳入本组」")
    vs = req.model_dump(include={'gateways', 'df_forward_type', 'df_forward_detail', 'df_forward_timeout'})
    bindings = {c.k8s_name: {'hosts': c.hosts} for c in req.clusters}
    username = _username(request)
    vs_id = await store.create_standard(group['id'], req.namespace, req.name, vs, [], bindings, username)
    if excluded:  # 确认后创建:组规则也排除的单独纳入,只是手动忽略过的取消忽略
        action = None if view.scope.rule(*key)['visible'] else S.OVERRIDE_INCLUDE
        await store.set_overrides(group['id'], {key: action}, username)
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
# 组的 VS 范围:忽略 / 取消忽略;查看 / 删除集群里的 VS
# ---------------------------------------------------------------------------
@handler
async def ignore_vs(request):
    """在本组忽略这些 VS:不再显示、不能纳管和下发;组标准(如果有)保留,取消忽略后恢复。集群里的 VS 不受影响。"""
    req = ScopeChangeRequest(**await _body(request))
    await _group(req.group_id)
    actions = {(item.namespace, item.name): S.OVERRIDE_IGNORE for item in req.items}
    await store.set_overrides(req.group_id, actions, _username(request))
    return _ok(msg=f'已忽略 {len(actions)} 个 VirtualService,可以在「查看忽略」里取消')


@handler
async def unignore_vs(request):
    """取消忽略:组规则本来就纳入的,去掉手动忽略;被组规则排除的,单独纳入本组(不受规则影响)。"""
    req = ScopeChangeRequest(**await _body(request))
    group = await _group(req.group_id)
    scope = _safe_scope(group['include_patterns'], group['ignore_patterns'], {})
    actions = {}
    for item in req.items:
        key = (item.namespace, item.name)
        actions[key] = None if scope.rule(*key)['visible'] else S.OVERRIDE_INCLUDE
    await store.set_overrides(req.group_id, actions, _username(request))
    included = sum(1 for action in actions.values() if action == S.OVERRIDE_INCLUDE)
    note = f',其中 {included} 个被组规则排除的已单独纳入本组' if included else ''
    return _ok(msg=f'已取消忽略 {len(actions)} 个 VirtualService{note}')


def _old_agent(resp) -> bool:
    """旧版 agent 没有查看单个 / 删除接口:本地服务回 404 纯文本,agent 解析 JSON 失败。"""
    return isinstance(resp, dict) and 'status' not in resp and 'unexpected mimetype' in str(resp.get('error', ''))


def _delete_error(k8s_name: str, resp) -> str:
    if _old_agent(resp):
        return f'集群 {k8s_name} 的 agent 版本过旧,还不支持删除 VirtualService,请先升级 agent'
    return _agent_error(resp)


async def _delete_in_cluster(k8s_name: str, namespace: str, name: str) -> Tuple[Optional[bool], dict]:
    """删除集群里的 VS(结果同 _agent_write);agent 回 404(已经没有了)也算删掉了。"""
    ok, resp = await _agent_write(k8s_name, AGENT_DELETE_PATH, 'DELETE', query={'namespace': namespace, 'name': name})
    if ok is False and isinstance(resp, dict) and resp.get('status') == 404:
        ok = True
    return ok, resp


async def _fetch_one(k8s_name: str, namespace: str, name: str) -> Tuple[Optional[dict], bool]:
    """实时从 agent 取集群里的一个 VS:(对象, 是否完整),集群里没有时对象为 None;对象去掉了 managedFields 和
    last-applied 注解。旧版 agent 还没有取单个 VS 的接口,用列表接口拼出来(只有 spec,不完整)。"""
    try:
        resp = await _call_agent(k8s_name, AGENT_DETAIL_PATH, query={'namespace': namespace, 'name': name},
                                 timeout=AGENT_TIMEOUT)
    except Exception as exc:
        raise IstioError(f'集群 {k8s_name}:{exc}', 502)
    if isinstance(resp, dict) and resp.get('success') is True and isinstance(resp.get('data'), dict):
        return S.clean_live(resp['data']), True
    if isinstance(resp, dict) and resp.get('status') == 404:
        return None, True
    if not _old_agent(resp):
        raise IstioError(f'从集群 {k8s_name} 取现状失败:{_agent_error(resp)}', 502)
    live = next((i for i in await fetch_live(k8s_name) if i['namespace'] == namespace and i['name'] == name), None)
    if live is None:
        return None, False
    obj = S.manifest(namespace, name, live['spec'])
    obj['metadata']['resourceVersion'] = live['resource_version']
    return obj, False


async def _live_target(request) -> Tuple[dict, str, str, str]:
    """查看集群里某个 VS 的参数:(组, 集群, 命名空间, 名称);集群要在组里,并且当前用户能看。"""
    query = request.query
    group = await _group(_int(query.get('group_id'), 'group_id'))
    k8s_name, namespace, name = (query.get(key) or '' for key in ('k8s_name', 'namespace', 'name'))
    if not k8s_name or not namespace or not name:
        raise IstioError('缺少参数: k8s_name / namespace / name')
    visible = {c['k8s_name'] for c in _visible(request, group)}
    _members(group, [k8s_name])
    if k8s_name not in visible:
        raise IstioError(f'没有集群 {k8s_name} 的访问权限', 403)
    return group, k8s_name, namespace, name


@handler
async def live_detail(request):
    """查看集群里这个 VS 的现状:实时取完整对象,去掉 managedFields 和 last-applied 注解(旧版 agent 只有 spec)。"""
    _, k8s_name, namespace, name = await _live_target(request)
    obj, full = await _fetch_one(k8s_name, namespace, name)
    if obj is None:
        raise IstioError(f'集群 {k8s_name} 里已经没有 {namespace}/{name}', 404)
    return _ok({'object': obj, 'full': full})


@handler
async def live_backups(request):
    """集群里这个 VS 的备份(新的在前)和实时取的集群现状(对比用,判断回退后有没有变化);
    现状取不到时 live 为空、live_error 说明原因,备份照样列出。"""
    _, k8s_name, namespace, name = await _live_target(request)
    backups = await store.list_backups(k8s_name, namespace, name)
    live, live_error = None, None
    try:
        obj, _ = await _fetch_one(k8s_name, namespace, name)
        live = {'exists': obj is not None, 'spec': (obj.get('spec') or {}) if obj else None,
                'resource_version': str(((obj or {}).get('metadata') or {}).get('resourceVersion') or '')}
    except IstioError as exc:
        live_error = exc.message
    for backup in backups:
        backup['exists'] = backup['spec'] is not None
        backup['same'] = live is not None and _same_spec(live['spec'], backup['spec'])
    return _ok({'keep': store.BACKUP_KEEP, 'live': live, 'live_error': live_error, 'backups': backups})


@handler
async def rollback_live(request):
    """把集群里的 VS 回退到某一份备份(备份时集群里还没有的,回退就是从集群删除)。只改集群,不改组标准,
    下次下发会再用组标准覆盖;回退前同样先备份集群当前的内容,回退也能撤销。"""
    req = RollbackRequest(**await _body(request))
    group = await _group(req.group_id)
    _members(group, [req.k8s_name])
    key, target = (req.k8s_name, req.namespace, req.name), f'{req.namespace}/{req.name}'
    backup = await store.get_backup(req.backup_id)
    if backup is None or (backup['k8s_name'], backup['namespace'], backup['name']) != key:
        raise IstioError(f'这份备份已经没有了(每个集群只留最近 {store.BACKUP_KEEP} 份),请刷新后重新选择', 404)
    username, ip = _username(request), client_ip(request)
    point = f"{_beijing(backup['created_at'])} {BACKUP_ACTION_TEXT[backup['action']]}前"
    live = next((i for i in await fetch_live(req.k8s_name)
                 if i['namespace'] == req.namespace and i['name'] == req.name), None)
    if _same_spec(live['spec'] if live else None, backup['spec']):
        return _ok(msg=f'集群 {req.k8s_name} 里的 {target} 已经和 {point}的内容一样,不需要回退')
    backup_id = await _backup(*key, live, 'rollback', username)
    if backup['spec'] is None:
        ok, resp = await _delete_in_cluster(*key)
    else:
        ok, resp = await _agent_write(req.k8s_name, AGENT_APPLY_PATH, 'POST',
                                      body=S.manifest(req.namespace, req.name, backup['spec']))
    # 结果不明时,要回退到的那份这次先不清理:万一没生效还能再回退
    await _settle_backup(backup_id, key, ok, keep_id=backup['id'] if ok is None else None)
    detail = {'group_id': group['id'], 'backup_id': backup['id'], 'backup_action': backup['action'],
              'backup_at': backup['created_at'].isoformat(), 'backup_by': backup['created_by'],
              'spec': live['spec'] if live else None}
    if not ok:
        error = _delete_error(req.k8s_name, resp) if backup['spec'] is None else _agent_error(resp)
        audit.record_bg(username, 'istio.rollback', env=req.k8s_name, target=target, ip=ip, success=False,
                        detail={**detail, 'error': error})
        raise IstioError(f'回退失败:{error}', 502)
    try:
        await store.replace_snapshots(req.k8s_name, await fetch_live(req.k8s_name), username)
    except Exception as exc:
        logger.warning(f'回退后刷新集群 {req.k8s_name} 的现状失败: {exc}')
        if backup['spec'] is None:
            await store.drop_snapshot(*key)
    audit.record_bg(username, 'istio.rollback', env=req.k8s_name, target=target, ip=ip, success=True, detail=detail)
    if backup['spec'] is None:
        done = f'{point}集群 {req.k8s_name} 里还没有 {target},已从集群删除'
    else:
        done = f'已把集群 {req.k8s_name} 里的 {target} 回退到 {point}的内容'
    standard = await store.find_standard(group['id'], req.namespace, req.name)
    if standard and req.k8s_name in standard['bindings']:
        done += ';组标准没有变,下次下发会再用组标准覆盖'
    return _ok(msg=done)


@handler
async def delete_live(request):
    """删除集群里的 VirtualService(立即生效)。删除前先备份(可以回退恢复),删除前的内容也记进审计 istio.delete;
    本组标准部署到了该集群的,同时不再部署(删掉该集群的 hosts 和域名),免得下次下发又建回来。"""
    req = LiveDeleteRequest(**await _body(request))
    group = await _group(req.group_id)
    _members(group, [req.k8s_name])
    username, ip = _username(request), client_ip(request)
    target = f'{req.namespace}/{req.name}'
    items = await fetch_live(req.k8s_name)
    live = next((i for i in items if i['namespace'] == req.namespace and i['name'] == req.name), None)
    detail = {'group_id': group['id'], 'spec': live['spec'] if live else None,
              'resource_version': live['resource_version'] if live else None}
    if live is not None:
        key = (req.k8s_name, req.namespace, req.name)
        backup_id = await _backup(*key, live, 'delete', username)
        ok, resp = await _delete_in_cluster(*key)
        await _settle_backup(backup_id, key, ok)
        if not ok:
            error = _delete_error(req.k8s_name, resp)
            audit.record_bg(username, 'istio.delete', env=req.k8s_name, target=target, ip=ip, success=False,
                            detail={**detail, 'error': error})
            raise IstioError(f'删除失败:{error}', 502)
    standard = await store.find_standard(group['id'], req.namespace, req.name)
    unbound = bool(standard and req.k8s_name in standard['bindings'])
    if unbound:
        await store.delete_binding(standard['id'], req.k8s_name)
    try:  # 集群里本来就没有的,刚取到的就是最新现状
        fresh = items if live is None else await fetch_live(req.k8s_name)
        await store.replace_snapshots(req.k8s_name, fresh, username)
    except Exception as exc:
        logger.warning(f'删除后刷新集群 {req.k8s_name} 的现状失败: {exc}')
        await store.drop_snapshot(req.k8s_name, req.namespace, req.name)
    if live is not None:
        audit.record_bg(username, 'istio.delete', env=req.k8s_name, target=target, ip=ip, success=True,
                        detail={**detail, 'unbound': unbound})
    message = f'已从集群 {req.k8s_name} 删除 {target}' if live is not None else f'集群 {req.k8s_name} 里已经没有 {target}'
    return _ok(msg=message + (',并且本组不再部署到该集群' if unbound else ''))


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
    """每一项都必须是组标准已纳入的集群;生成要下发的 spec。不能下发的(在本组被忽略、和其他组冲突、
    规则的域名没设置全)error 说明原因。"""
    standards: Dict[int, dict] = {}
    views: Dict[int, GroupView] = {}
    prepared = []
    for item in items:
        if item.vs_id not in standards:
            standards[item.vs_id] = await _standard(item.vs_id)
        standard = standards[item.vs_id]
        if standard['group_id'] not in views:
            views[standard['group_id']] = await _view_of(await _group(standard['group_id']))
        view = views[standard['group_id']]
        binding = standard['bindings'].get(item.k8s_name)
        if binding is None:
            raise IstioError(f"{standard['namespace']}/{standard['name']} 没有部署到集群 {item.k8s_name}")
        rendered = S.standard_spec(standard, binding['hosts'], item.k8s_name)
        key = (standard['namespace'], standard['name'])
        scope, conflicts = view.classify(key), view.conflicts([key]).get(key)
        missing = S.missing_domains(standard, item.k8s_name)
        if not scope['visible']:
            error = f'{_scope_text(scope)},不能下发;如需下发,先在「查看忽略」里取消忽略'
        elif conflicts:
            error = _conflict_error(key, conflicts)
        elif missing:
            error = f"还没设置 {item.k8s_name} 的域名:{'、'.join(missing)}"
        else:
            error = None
        prepared.append({
            'vs_id': standard['id'], 'k8s_name': item.k8s_name,
            'namespace': standard['namespace'], 'name': standard['name'],
            'spec': rendered, 'hash': S.spec_hash(rendered), 'error': error,
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


def _apply_result(item: dict, username: str, ip: str, error: Optional[str] = None) -> dict:
    """记审计 istio.apply,返回这一项的下发结果。"""
    audit.record_bg(username, 'istio.apply', env=item['k8s_name'], target=f"{item['namespace']}/{item['name']}",
                    ip=ip, success=error is None, detail={'vs_id': item['vs_id'], **({'error': error} if error else {})})
    return {'vs_id': item['vs_id'], 'k8s_name': item['k8s_name'], 'namespace': item['namespace'],
            'name': item['name'], 'success': error is None, 'error': error}


async def _apply_one(item: dict, live: Optional[dict], username: str, ip: str) -> dict:
    """下发一项。集群里的内容会变时先备份当时的内容(live,None 表示集群里没有),备份不了就不下发;
    agent 明确回了失败(集群没变)就删掉这份备份。"""
    key = (item['k8s_name'], item['namespace'], item['name'])
    backup_id = None
    if live is None or S.spec_hash(live['spec']) != item['hash']:
        try:
            backup_id = await _backup(*key, live, 'apply', username)
        except Exception as exc:
            logger.exception(f'下发前备份 {key} 失败: {exc}')
            return _apply_result(item, username, ip, f'下发前备份集群里的内容失败,没有下发:{exc}')
    ok, resp = await _agent_write(item['k8s_name'], AGENT_APPLY_PATH, 'POST',
                                  body=S.manifest(item['namespace'], item['name'], item['spec']))
    if ok:
        await store.mark_applied(item['vs_id'], item['k8s_name'], item['hash'], username)
    if backup_id is not None:
        await _settle_backup(backup_id, key, ok)
    return _apply_result(item, username, ip, None if ok else _agent_error(resp))


@handler
async def apply(request):
    """按组标准 + 各集群 hosts 下发(agent 用 Server-Side Apply)。同一集群依次下发,不同集群并行;
    下发前从集群取最新现状备份(取不到就不下发这个集群),下发完重新采集该集群现状,状态以集群里的实际内容为准。"""
    req = ApplyRequest(**await _body(request))
    prepared = await _prepare(req.items)
    blocked = [f"{i['namespace']}/{i['name']} → {i['error']}" for i in prepared if i['error']]
    if blocked:
        raise IstioError('；'.join(blocked))
    username, ip = _username(request), client_ip(request)

    async def run(k8s_name, items):
        try:
            current = {(i['namespace'], i['name']): i for i in await fetch_live(k8s_name)}
        except IstioError as exc:
            error = f'下发前取集群现状(用于备份)失败,没有下发:{exc.message}'
            return [_apply_result(item, username, ip, error) for item in items]
        results = [await _apply_one(item, current.get((item['namespace'], item['name'])), username, ip)
                   for item in items]
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
    router.add_get('/api/istio/groups/preview', preview_group)
    router.add_get('/api/istio/matrix', group_matrix)
    router.add_post('/api/istio/collect', collect)
    router.add_get('/api/istio/compare', compare)
    router.add_post('/api/istio/import', import_standards)
    router.add_post('/api/istio/ignore', ignore_vs)
    router.add_post('/api/istio/unignore', unignore_vs)
    router.add_get('/api/istio/live', live_detail)
    router.add_post('/api/istio/live/delete', delete_live)
    router.add_get('/api/istio/live/backups', live_backups)
    router.add_post('/api/istio/live/rollback', rollback_live)
    router.add_post('/api/istio/vs/reset', reset_standard)
    router.add_post('/api/istio/vs/sync/preview', sync_preview)
    router.add_post('/api/istio/vs/sync', sync_to_standard)
    router.add_get('/api/istio/vs/check', check_vs)
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
