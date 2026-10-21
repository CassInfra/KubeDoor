"""Istio 管理的数据访问(asyncpg,经 db.py 连接池)。

JSON 列都是 jsonb:写入时 json.dumps 再 ::jsonb,读出时 json.loads(连接池是共用的,不注册 jsonb 编解码器)。
"""

import json
from typing import Dict, Iterable, List, Optional

import asyncpg

import db


class IstioError(Exception):
    """业务错误:message 直接给前端看,status 是 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _dump(value) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _load(value):
    return json.loads(value) if isinstance(value, str) else value


_GROUP_COLS = 'id, name, description, include_patterns, ignore_patterns, created_at, updated_at'
# 组成员 + 该集群的采集状态(采集按集群,各组共用)
_MEMBER_SQL = ('SELECT m.group_id, m.k8s_name, m.env_type, m.remark, m.sort_order, s.collected_at, '
               "COALESCE(s.collected_by, '') AS collected_by, COALESCE(s.vs_count, 0) AS vs_count "
               'FROM istio_group_cluster m LEFT JOIN istio_cluster_state s ON s.k8s_name = m.k8s_name')
_VS_COLS = ('id, group_id, namespace, name, gateways, df_forward_type, df_forward_detail, df_forward_timeout, '
            'df_extra, extra_spec, updated_by, created_at, updated_at')
_ROUTE_COLS = ('r.id, r.vs_id, r.priority, r.name, r.match_rules, r.rewrite_rules, r.forward_type, r.forward_detail, '
               'r.timeout, r.extra, r.created_at, r.updated_at')
_BINDING_COLS = 'b.vs_id, b.k8s_name, b.hosts, b.applied_hash, b.applied_at, b.applied_by'


def _group(row, members) -> dict:
    data = dict(row)
    data['include_patterns'] = _load(data['include_patterns']) or []
    data['ignore_patterns'] = _load(data['ignore_patterns']) or []
    data['clusters'] = members
    return data


def _member(row) -> dict:
    data = dict(row)
    data.pop('group_id', None)
    return data


def _vs(row) -> dict:
    data = dict(row)
    for key in ('gateways', 'df_forward_detail', 'df_extra', 'extra_spec'):
        data[key] = _load(data[key])
    data['gateways'] = data['gateways'] or []
    data['routes'], data['bindings'] = [], {}
    return data


def _route(row) -> dict:
    data = dict(row)
    for key in ('match_rules', 'rewrite_rules', 'forward_detail', 'extra'):
        data[key] = _load(data[key])
    data['domains'] = {}  # {k8s_name: 各域名块的域名},见 istio_vs_route_domain
    return data


def _binding(row) -> dict:
    data = dict(row)
    data['hosts'] = _load(data['hosts']) or []
    return data


# ---------------------------------------------------------------------------
# Istio 组
# ---------------------------------------------------------------------------
async def list_groups() -> List[dict]:
    groups = await db.pg_fetch(f'SELECT {_GROUP_COLS} FROM istio_group ORDER BY name')
    members = await db.pg_fetch(f'{_MEMBER_SQL} ORDER BY m.group_id, m.sort_order, m.k8s_name')
    by_group: Dict[int, List[dict]] = {}
    for row in members:
        by_group.setdefault(row['group_id'], []).append(_member(row))
    return [_group(row, by_group.get(row['id'], [])) for row in groups]


async def get_group(group_id: int) -> Optional[dict]:
    row = await db.pg_fetchrow(f'SELECT {_GROUP_COLS} FROM istio_group WHERE id = $1', group_id)
    if row is None:
        return None
    members = await db.pg_fetch(f'{_MEMBER_SQL} WHERE m.group_id = $1 ORDER BY m.sort_order, m.k8s_name', group_id)
    return _group(row, [_member(m) for m in members])


async def _drop_orphan_clusters(conn) -> None:
    """不在任何组里的集群:采集到的现状和采集状态一并删除。"""
    await conn.execute(
        'DELETE FROM istio_vs_snapshot WHERE k8s_name NOT IN (SELECT k8s_name FROM istio_group_cluster)')
    await conn.execute(
        'DELETE FROM istio_cluster_state WHERE k8s_name NOT IN (SELECT k8s_name FROM istio_group_cluster)')


async def save_group(group_id: Optional[int], name: str, description: str, clusters: List[dict],
                     include_patterns: List[dict], ignore_patterns: List[dict]) -> int:
    """新建(group_id 为空)或修改组。clusters 按显示顺序:[{k8s_name, env_type, remark}];一个集群可以属于多个组。

    移出组的集群,它在本组 VS 里的 hosts 和域名一并删除;不再属于任何组的集群,采集到的现状也删除。"""
    names = [c['k8s_name'] for c in clusters]
    patterns = (_dump(include_patterns or []), _dump(ignore_patterns or []))
    try:
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():
                if group_id is None:
                    group_id = await conn.fetchval(
                        'INSERT INTO istio_group (name, description, include_patterns, ignore_patterns) '
                        'VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id', name, description, *patterns)
                else:
                    status = await conn.execute(
                        'UPDATE istio_group SET name = $2, description = $3, include_patterns = $4::jsonb, '
                        'ignore_patterns = $5::jsonb WHERE id = $1', group_id, name, description, *patterns)
                    if status == 'UPDATE 0':
                        raise IstioError('Istio 组不存在', 404)
                await conn.execute(
                    'DELETE FROM istio_vs_cluster b USING istio_vs v WHERE b.vs_id = v.id AND v.group_id = $1 '
                    'AND NOT (b.k8s_name = ANY($2::text[]))', group_id, names)
                await conn.execute(
                    'DELETE FROM istio_group_cluster WHERE group_id = $1 AND NOT (k8s_name = ANY($2::text[]))',
                    group_id, names)
                for index, cluster in enumerate(clusters):
                    await conn.execute(
                        'INSERT INTO istio_group_cluster (group_id, k8s_name, env_type, remark, sort_order) '
                        'VALUES ($1, $2, $3, $4, $5) ON CONFLICT (group_id, k8s_name) DO UPDATE SET '
                        'env_type = EXCLUDED.env_type, remark = EXCLUDED.remark, sort_order = EXCLUDED.sort_order',
                        group_id, cluster['k8s_name'], cluster['env_type'], cluster.get('remark') or '', index)
                await _drop_orphan_clusters(conn)
    except asyncpg.UniqueViolationError:
        raise IstioError(f'组名 {name} 已存在')
    return group_id


async def delete_group(group_id: int) -> bool:
    """删除组:组标准、各集群 hosts 和域名、手动忽略记录都会删除,只在这个组里的集群的现状也删除;
    集群里的 VirtualService 不受影响。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            status = await conn.execute('DELETE FROM istio_group WHERE id = $1', group_id)
            await _drop_orphan_clusters(conn)
    return status != 'DELETE 0'


# ---------------------------------------------------------------------------
# 组的 VS 范围:单个 VS 的手动忽略 / 单独纳入
# ---------------------------------------------------------------------------
async def load_overrides(group_ids: Iterable[int]) -> Dict[int, Dict[tuple, dict]]:
    """{group_id: {(namespace, name): {action, created_by, created_at}}}"""
    ids = list(group_ids)
    result: Dict[int, Dict[tuple, dict]] = {group_id: {} for group_id in ids}
    if not ids:
        return result
    rows = await db.pg_fetch(
        'SELECT group_id, namespace, name, action, created_by, created_at FROM istio_group_vs_override '
        'WHERE group_id = ANY($1::int[])', ids)
    for row in rows:
        result[row['group_id']][(row['namespace'], row['name'])] = {
            'action': row['action'], 'created_by': row['created_by'], 'created_at': row['created_at']}
    return result


async def set_overrides(group_id: int, actions: Dict[tuple, Optional[str]], username: str) -> None:
    """{(namespace, name): 'ignore' | 'include' | None},None 表示删掉手动决定(回到按组规则)。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            for (namespace, name), action in actions.items():
                if action is None:
                    await conn.execute(
                        'DELETE FROM istio_group_vs_override WHERE group_id = $1 AND namespace = $2 AND name = $3',
                        group_id, namespace, name)
                else:
                    await conn.execute(
                        'INSERT INTO istio_group_vs_override (group_id, namespace, name, action, created_by) '
                        'VALUES ($1, $2, $3, $4, $5) ON CONFLICT (group_id, namespace, name) DO UPDATE SET '
                        'action = EXCLUDED.action, created_by = EXCLUDED.created_by, created_at = now()',
                        group_id, namespace, name, action, username)


async def standard_keys(group_ids: Iterable[int]) -> Dict[int, set]:
    """各组已有组标准的 VS:{group_id: {(namespace, name)}}"""
    ids = list(group_ids)
    result: Dict[int, set] = {group_id: set() for group_id in ids}
    if ids:
        for row in await db.pg_fetch(
                'SELECT group_id, namespace, name FROM istio_vs WHERE group_id = ANY($1::int[])', ids):
            result[row['group_id']].add((row['namespace'], row['name']))
    return result


async def snapshot_keys(k8s_names: Iterable[str]) -> Dict[str, set]:
    """各集群现状里有哪些 VS:{k8s_name: {(namespace, name)}}"""
    names = list(k8s_names)
    result: Dict[str, set] = {name: set() for name in names}
    if names:
        for row in await db.pg_fetch(
                'SELECT k8s_name, namespace, name FROM istio_vs_snapshot WHERE k8s_name = ANY($1::text[])', names):
            result[row['k8s_name']].add((row['namespace'], row['name']))
    return result


async def collected_clusters(k8s_names: Iterable[str]) -> set:
    names = list(k8s_names)
    if not names:
        return set()
    rows = await db.pg_fetch(
        'SELECT k8s_name FROM istio_cluster_state WHERE k8s_name = ANY($1::text[]) AND collected_at IS NOT NULL', names)
    return {row['k8s_name'] for row in rows}


# ---------------------------------------------------------------------------
# 集群现状(采集结果)
# ---------------------------------------------------------------------------
async def replace_snapshots(k8s_name: str, items: List[dict], username: str) -> None:
    """用采集结果整体替换该集群的现状(各组共用)。items:[{namespace, name, spec, resource_version}]。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute('DELETE FROM istio_vs_snapshot WHERE k8s_name = $1', k8s_name)
            if items:
                await conn.executemany(
                    'INSERT INTO istio_vs_snapshot (k8s_name, namespace, name, spec, resource_version) '
                    'VALUES ($1, $2, $3, $4::jsonb, $5)',
                    [(k8s_name, item['namespace'], item['name'], _dump(item['spec']),
                      item.get('resource_version') or '') for item in items])
            await conn.execute(
                'INSERT INTO istio_cluster_state (k8s_name, collected_at, collected_by, vs_count) '
                'VALUES ($1, now(), $2, $3) ON CONFLICT (k8s_name) DO UPDATE SET '
                'collected_at = now(), collected_by = EXCLUDED.collected_by, vs_count = EXCLUDED.vs_count',
                k8s_name, username, len(items))


async def drop_snapshot(k8s_name: str, namespace: str, name: str) -> None:
    """集群里删掉了某个 VS(重新采集失败时用):只从现状里去掉这一条。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            status = await conn.execute(
                'DELETE FROM istio_vs_snapshot WHERE k8s_name = $1 AND namespace = $2 AND name = $3',
                k8s_name, namespace, name)
            if status != 'DELETE 0':
                await conn.execute('UPDATE istio_cluster_state SET vs_count = GREATEST(vs_count - 1, 0) '
                                   'WHERE k8s_name = $1', k8s_name)


async def load_snapshots(k8s_names: Iterable[str], namespace: str = None, name: str = None) -> Dict[tuple, dict]:
    """{(k8s_name, namespace, name): spec};给了 namespace / name 时只取这一个 VS。"""
    names = list(k8s_names)
    if not names:
        return {}
    if namespace is not None and name is not None:
        rows = await db.pg_fetch(
            'SELECT k8s_name, namespace, name, spec FROM istio_vs_snapshot '
            'WHERE k8s_name = ANY($1::text[]) AND namespace = $2 AND name = $3', names, namespace, name)
    else:
        rows = await db.pg_fetch(
            'SELECT k8s_name, namespace, name, spec FROM istio_vs_snapshot WHERE k8s_name = ANY($1::text[])', names)
    return {(r['k8s_name'], r['namespace'], r['name']): _load(r['spec']) for r in rows}


# ---------------------------------------------------------------------------
# 组标准 VS
# ---------------------------------------------------------------------------
def _assemble(vs_rows, route_rows, binding_rows, domain_rows) -> List[dict]:
    items = {row['id']: _vs(row) for row in vs_rows}
    routes = {}
    for row in route_rows:
        if row['vs_id'] in items:
            route = routes[row['id']] = _route(row)
            items[row['vs_id']]['routes'].append(route)
    for row in binding_rows:
        if row['vs_id'] in items:
            binding = _binding(row)
            items[row['vs_id']]['bindings'][binding['k8s_name']] = binding
    for row in domain_rows:
        if row['route_id'] in routes:
            routes[row['route_id']]['domains'][row['k8s_name']] = _load(row['domains']) or []
    return list(items.values())


async def list_standards(group_id: int) -> List[dict]:
    """组里所有组标准(含规则、各集群绑定和规则在各集群的域名),用于组矩阵。"""
    vs_rows = await db.pg_fetch(
        f'SELECT {_VS_COLS} FROM istio_vs WHERE group_id = $1 ORDER BY namespace, name', group_id)
    route_rows = await db.pg_fetch(
        f'SELECT {_ROUTE_COLS} FROM istio_vs_route r JOIN istio_vs v ON v.id = r.vs_id '
        'WHERE v.group_id = $1 ORDER BY r.vs_id, r.priority, r.id', group_id)
    binding_rows = await db.pg_fetch(
        f'SELECT {_BINDING_COLS} FROM istio_vs_cluster b JOIN istio_vs v ON v.id = b.vs_id WHERE v.group_id = $1',
        group_id)
    domain_rows = await db.pg_fetch(
        'SELECT d.route_id, d.k8s_name, d.domains FROM istio_vs_route_domain d '
        'JOIN istio_vs v ON v.id = d.vs_id WHERE v.group_id = $1', group_id)
    return _assemble(vs_rows, route_rows, binding_rows, domain_rows)


async def get_standard(vs_id: int) -> Optional[dict]:
    vs_rows = await db.pg_fetch(f'SELECT {_VS_COLS} FROM istio_vs WHERE id = $1', vs_id)
    if not vs_rows:
        return None
    route_rows = await db.pg_fetch(
        f'SELECT {_ROUTE_COLS} FROM istio_vs_route r WHERE r.vs_id = $1 ORDER BY r.priority, r.id', vs_id)
    binding_rows = await db.pg_fetch(
        f'SELECT {_BINDING_COLS} FROM istio_vs_cluster b WHERE b.vs_id = $1 ORDER BY b.k8s_name', vs_id)
    domain_rows = await db.pg_fetch(
        'SELECT route_id, k8s_name, domains FROM istio_vs_route_domain WHERE vs_id = $1', vs_id)
    return _assemble(vs_rows, route_rows, binding_rows, domain_rows)[0]


async def find_standard(group_id: int, namespace: str, name: str) -> Optional[dict]:
    vs_id = await db.pg_fetchval(
        'SELECT id FROM istio_vs WHERE group_id = $1 AND namespace = $2 AND name = $3', group_id, namespace, name)
    return await get_standard(vs_id) if vs_id is not None else None


async def route_vs_id(route_id: int) -> Optional[int]:
    return await db.pg_fetchval('SELECT vs_id FROM istio_vs_route WHERE id = $1', route_id)


async def _save_domains(conn, route_id: int, domains: Dict[str, list]) -> None:
    """整体替换一条规则在各集群的域名({k8s_name: 各域名块的域名});集群必须已纳入该 VS(外键)。"""
    await conn.execute('DELETE FROM istio_vs_route_domain WHERE route_id = $1', route_id)
    if not domains:
        return
    try:
        await conn.executemany(
            'INSERT INTO istio_vs_route_domain (route_id, vs_id, k8s_name, domains) '
            'SELECT id, vs_id, $2, $3::jsonb FROM istio_vs_route WHERE id = $1',
            [(route_id, k8s_name, _dump(lists)) for k8s_name, lists in domains.items()])
    except asyncpg.ForeignKeyViolationError:
        raise IstioError(f"VirtualService 没有部署到集群 {'、'.join(domains)} 中的某个,请刷新后重试")


async def _insert_routes(conn, vs_id: int, routes: List[dict]) -> None:
    for route in routes:
        route_id = await conn.fetchval(
            'INSERT INTO istio_vs_route (vs_id, priority, name, match_rules, rewrite_rules, forward_type, '
            'forward_detail, timeout, extra) VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7::jsonb, $8, $9::jsonb) '
            'RETURNING id',
            vs_id, route['priority'], route.get('name'), _dump(route.get('match_rules')),
            _dump(route.get('rewrite_rules')), route.get('forward_type'), _dump(route.get('forward_detail')),
            route.get('timeout'), _dump(route.get('extra')))
        if route.get('domains'):
            await _save_domains(conn, route_id, route['domains'])


_VS_FIELDS_SQL = ('gateways = $2::jsonb, df_forward_type = $3, df_forward_detail = $4::jsonb, '
                  'df_forward_timeout = $5, df_extra = $6::jsonb, extra_spec = $7::jsonb, updated_by = $8')


def _vs_args(vs: dict, username: str) -> tuple:
    return (_dump(vs.get('gateways') or []), vs.get('df_forward_type'), _dump(vs.get('df_forward_detail')),
            vs.get('df_forward_timeout'), _dump(vs.get('df_extra')), _dump(vs.get('extra_spec')), username)


async def create_standard(group_id: int, namespace: str, name: str, vs: dict, routes: List[dict],
                          bindings: Dict[str, dict], username: str) -> int:
    """新建组标准。bindings:{k8s_name: {hosts, applied_hash}},applied_hash 是纳管时该集群的现状(没有就空);
    routes 里的 domains 是各集群的域名({k8s_name: 各域名块的域名}),集群要在 bindings 里。"""
    try:
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():
                vs_id = await conn.fetchval(
                    'INSERT INTO istio_vs (group_id, namespace, name, gateways, df_forward_type, df_forward_detail, '
                    'df_forward_timeout, df_extra, extra_spec, updated_by) '
                    'VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb, $7, $8::jsonb, $9::jsonb, $10) RETURNING id',
                    group_id, namespace, name, *_vs_args(vs, username))
                for k8s_name, binding in bindings.items():
                    await conn.execute(
                        'INSERT INTO istio_vs_cluster (vs_id, k8s_name, hosts, applied_hash) VALUES ($1, $2, $3::jsonb, $4)',
                        vs_id, k8s_name, _dump(binding.get('hosts') or []), binding.get('applied_hash'))
                await _insert_routes(conn, vs_id, routes)
    except asyncpg.UniqueViolationError:
        raise IstioError(f'组里已经有 {namespace}/{name} 的组标准')
    return vs_id


async def replace_standard_content(vs_id: int, vs: dict, routes: List[dict], username: str) -> None:
    """用某个集群的现状整体替换组标准的共用部分(规则、默认路由、gateways),各集群 hosts 不变;
    routes 里带各集群的域名。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            status = await conn.execute(f'UPDATE istio_vs SET {_VS_FIELDS_SQL} WHERE id = $1', vs_id,
                                        *_vs_args(vs, username))
            if status == 'UPDATE 0':
                raise IstioError('VirtualService 不存在', 404)
            await conn.execute('DELETE FROM istio_vs_route WHERE vs_id = $1', vs_id)
            await _insert_routes(conn, vs_id, routes)


async def update_standard_fields(vs_id: int, fields: dict, username: str) -> None:
    """修改 gateways 和默认路由的 route / delegate、超时;默认路由里未建模的字段和 extra_spec 保持不变。"""
    status = await db.pg_execute(
        'UPDATE istio_vs SET gateways = $2::jsonb, df_forward_type = $3, df_forward_detail = $4::jsonb, '
        'df_forward_timeout = $5, updated_by = $6 WHERE id = $1',
        vs_id, _dump(fields.get('gateways') or []), fields.get('df_forward_type'),
        _dump(fields.get('df_forward_detail')), fields.get('df_forward_timeout'), username)
    if status == 'UPDATE 0':
        raise IstioError('VirtualService 不存在', 404)


async def delete_standard(vs_id: int) -> None:
    status = await db.pg_execute('DELETE FROM istio_vs WHERE id = $1', vs_id)
    if status == 'DELETE 0':
        raise IstioError('VirtualService 不存在', 404)


# ---------------------------------------------------------------------------
# VS 部署到哪些集群(hosts 各集群独立)
# ---------------------------------------------------------------------------
async def upsert_binding(vs_id: int, k8s_name: str, hosts: List[str],
                         domains: Optional[Dict[int, list]] = None) -> None:
    """部署到某集群 / 修改它的 hosts;domains({规则 id: 各域名块的域名})给了就一起保存该集群在这些规则里的域名。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                'INSERT INTO istio_vs_cluster (vs_id, k8s_name, hosts) VALUES ($1, $2, $3::jsonb) '
                'ON CONFLICT (vs_id, k8s_name) DO UPDATE SET hosts = EXCLUDED.hosts',
                vs_id, k8s_name, _dump(hosts))
            for route_id, lists in (domains or {}).items():
                await conn.execute(
                    'INSERT INTO istio_vs_route_domain (route_id, vs_id, k8s_name, domains) '
                    'SELECT id, vs_id, $2, $3::jsonb FROM istio_vs_route WHERE id = $1 AND vs_id = $4 '
                    'ON CONFLICT (route_id, k8s_name) DO UPDATE SET domains = EXCLUDED.domains',
                    route_id, k8s_name, _dump(lists), vs_id)


async def delete_binding(vs_id: int, k8s_name: str) -> None:
    status = await db.pg_execute('DELETE FROM istio_vs_cluster WHERE vs_id = $1 AND k8s_name = $2', vs_id, k8s_name)
    if status == 'DELETE 0':
        raise IstioError(f'该 VirtualService 没有部署到集群 {k8s_name}', 404)


async def mark_applied(vs_id: int, k8s_name: str, applied_hash: str, username: str) -> None:
    await db.pg_execute(
        'UPDATE istio_vs_cluster SET applied_hash = $3, applied_at = now(), applied_by = $4 '
        'WHERE vs_id = $1 AND k8s_name = $2', vs_id, k8s_name, applied_hash, username)


# ---------------------------------------------------------------------------
# 组标准的 http 规则
# ---------------------------------------------------------------------------
async def _route_vs_id(conn, route_id: int) -> int:
    vs_id = await conn.fetchval('SELECT vs_id FROM istio_vs_route WHERE id = $1 FOR UPDATE', route_id)
    if vs_id is None:
        raise IstioError('规则不存在', 404)
    return vs_id


async def _check_priority(conn, vs_id: int, priority: int, route_id: int = 0) -> None:
    taken = await conn.fetchval(
        'SELECT 1 FROM istio_vs_route WHERE vs_id = $1 AND priority = $2 AND id <> $3', vs_id, priority, route_id)
    if taken:
        raise IstioError(f'优先级 {priority} 已被其他规则占用')


async def _touch_vs(conn, vs_id: int, username: str) -> None:
    """规则变了就是组标准变了:更新 VS 的修改人和时间(触发器维护 updated_at)。"""
    await conn.execute('UPDATE istio_vs SET updated_by = $2 WHERE id = $1', vs_id, username)


async def _renumber(conn, vs_id: int) -> None:
    await conn.execute(
        'UPDATE istio_vs_route r SET priority = s.rn * 10 FROM ('
        '  SELECT id, row_number() OVER (ORDER BY priority, id) AS rn FROM istio_vs_route WHERE vs_id = $1'
        ') s WHERE r.id = s.id AND r.priority <> s.rn * 10', vs_id)


async def create_route(vs_id: int, route: dict, username: str) -> int:
    """新增规则;route['domains'] 是各集群的域名(集群必须已纳入该 VS)。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval('SELECT 1 FROM istio_vs WHERE id = $1 FOR UPDATE', vs_id) is None:
                raise IstioError('VirtualService 不存在', 404)
            priority = route.get('priority')
            if priority is None:
                priority = await conn.fetchval(
                    'SELECT COALESCE(MAX(priority), 0) + 10 FROM istio_vs_route WHERE vs_id = $1', vs_id)
            else:
                await _check_priority(conn, vs_id, priority)
            route_id = await conn.fetchval(
                'INSERT INTO istio_vs_route (vs_id, priority, name, match_rules, rewrite_rules, forward_type, '
                'forward_detail, timeout) VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7::jsonb, $8) RETURNING id',
                vs_id, priority, route.get('name'), _dump(route.get('match_rules')), _dump(route.get('rewrite_rules')),
                route.get('forward_type'), _dump(route.get('forward_detail')), route.get('timeout'))
            await _save_domains(conn, route_id, route.get('domains') or {})
            await _touch_vs(conn, vs_id, username)
    return route_id


async def update_route(route_id: int, route: dict, username: str) -> int:
    """修改规则;规则里未建模的字段(extra)保持不变。priority 不传就保持原位置;各集群的域名整体替换。返回所属 VS。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            vs_id = await _route_vs_id(conn, route_id)
            priority = route.get('priority')
            if priority is not None:
                await _check_priority(conn, vs_id, priority, route_id)
            await conn.execute(
                'UPDATE istio_vs_route SET name = $2, match_rules = $3::jsonb, rewrite_rules = $4::jsonb, '
                'forward_type = $5, forward_detail = $6::jsonb, timeout = $7, priority = COALESCE($8, priority) '
                'WHERE id = $1',
                route_id, route.get('name'), _dump(route.get('match_rules')), _dump(route.get('rewrite_rules')),
                route.get('forward_type'), _dump(route.get('forward_detail')), route.get('timeout'), priority)
            await _save_domains(conn, route_id, route.get('domains') or {})
            await _touch_vs(conn, vs_id, username)
    return vs_id


async def delete_route(route_id: int, username: str) -> int:
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            vs_id = await _route_vs_id(conn, route_id)
            await conn.execute('DELETE FROM istio_vs_route WHERE id = $1', route_id)
            await _renumber(conn, vs_id)
            await _touch_vs(conn, vs_id, username)
    return vs_id


async def reorder_routes(vs_id: int, username: str) -> None:
    """把优先级重新整理成 10、20、30……(顺序不变),方便在中间插入。"""
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval('SELECT 1 FROM istio_vs WHERE id = $1 FOR UPDATE', vs_id) is None:
                raise IstioError('VirtualService 不存在', 404)
            await _renumber(conn, vs_id)
