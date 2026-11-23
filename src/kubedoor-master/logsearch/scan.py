"""扫描数据源、生成配置建议（约定 §10）：配置页「智能配置」用，只读，整体限时 90 秒，到点返回已扫到的（partial）。

ClickHouse：system.tables（Distributed 表解析出本地表）→ 行数和时间范围（clusterAllReplicas(…, system.parts)，
失败退回 total_rows）→ system.columns（按「字段名:类型」分组）→ 每组第一个目标取样本 2 条和低基数字段的 topK，
其余目标只取候选集群字段的 topK。
Elasticsearch：GET /、_resolve/index（失败退回 _cat/indices）→ 目标（data stream、别名、按名字归成模式的独立索引）
→ _field_caps（按字段集合分组）、_count、时间范围、模板说明、保留天数 → 每组第一个目标取样本、keyword 字段的取值、
正文里的 ID 写法。
样本和取值都经过打码（内置敏感名），每个值截断到 300 字。
"""

import asyncio
import fnmatch
import hashlib
import json
import re
import time

from . import schema as field_schema
from .clickhouse import ClickHouseClient, Params, kind_of, quoted
from .clickhouse import rows as ch_rows
from .common import LogError, clip_value, iso_ms, to_int
from .context import flatten
from .elastic import ElasticClient, describe_info, target_path
from .mask import Masker

SCAN_SECONDS = 90
STEP_TIMEOUT = 15
CH_TABLES = 3000
ES_TARGETS = 80
OTHER_TARGETS = 20
COLUMNS = 300
TOPK_FIELDS = 6
VALUE_CHARS = 300
TEXT_ID_SAMPLES = 50
CH_EXCLUDE = ('system', 'INFORMATION_SCHEMA', 'information_schema')
GENERIC_WORDS = frozenset({'prod', 'test', 'beta', 'dev', 'log', 'logs', 'access', 'uat', 'stg', 'staging', 'pre',
                           'local', 'dist', 'all', 'std', 'app', 'apps', 'data', 'cluster', 'k8s', 'kube', 'default',
                           'online', 'offline', 'main', 'new', 'old'})
CANDIDATES = {
    'time': ('@timestamp', 'timestamp', 'time', 'start_time', 'event_time', 'ts', 'datetime', 'date'),
    'message': ('message', 'msg', 'log', 'content', 'body'),
    'level': ('level', 'severity', 'loglevel', 'log_level', 'lvl'),
    'namespace': ('namespace', 'k8s_namespace', 'kubernetes.namespace', 'ns'),
    'pod': ('podname', 'pod', 'pod_name', 'k8s_pod', 'kubernetes.pod.name', 'hostname'),
    'service': ('appname', 'app', 'service', 'service_name', 'upstream_cluster', 'application'),
    'status': ('status', 'status_code', 'response_code', 'http_status'),
    'duration': ('duration', 'latency', 'responsetime', 'response_time', 'request_time', 'elapsed', 'cost', 'took',
                 'upstream_service_time'),
    'method': ('method', 'request_method', 'http_method'),
    'path': ('path', 'uri', 'url', 'pages', 'request_uri', 'url_path'),
    'host': ('host', 'authority', 'domain', 'server_name', 'http_host'),
    'client_ip': ('client_ip', 'xff', 'x_forwarded_for', 'remote_addr', 'remote_ip', 'ip'),
}
CLUSTER_NAMES = ('env', 'cluster', 'k8s_cluster', 'cluster_name', 'environment', 'k8sname')
ID_WORDS = ('trace', 'request', 'access', 'span', 'correlation')
_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
_UUID_RE = re.compile(_UUID)
_HEX_RE = re.compile(r'[0-9a-fA-F]{16,64}')
_TEXT_ID = re.compile(r'(?<![A-Za-z_])([A-Za-z_]+)([:=])\s*([0-9a-f]{16,64}|' + _UUID + r')(?![0-9A-Za-z])')
_JSON_ID = re.compile(r'"(\w*(?:trace|request|access)\w*)"(\s*:\s*)"([^"]{1,128})"', re.IGNORECASE)
_DISTRIBUTED = re.compile(r"Distributed\(\s*'?([^',\s)]+)'?\s*,\s*'?([^',\s)]*)'?\s*,\s*'?([^',\s)]+)'?")
_TTL_DAYS = re.compile(r'\bTTL\b.*?(?:toIntervalDay\((\d+)\)|INTERVAL\s+(\d+)\s+DAY)', re.IGNORECASE | re.DOTALL)
_SAFE_CLUSTER = re.compile(r'[A-Za-z0-9_.\-]{1,128}')
_DURATION = re.compile(r'(\d+)(d|h|m|s|ms|micros|nanos)$')
_DIGITS = re.compile(r'\d+')
_DATE_LIKE = re.compile(r'(?<![0-9])(?:19|20)\d{2}[._-]?\d{2}(?:[._-]?\d{2})?(?![0-9])')
ENV_WORDS = ('beta', 'test', 'dev', 'uat', 'stg', 'staging', 'pre', 'sit', 'qa', 'gray', 'canary')
MIN_TABLE_SCORE = 1.0
_LOG_FILE = re.compile(r'^/var/log/|\.log$')
_STAR_RUN = re.compile(r'\*(?:[.\-_]*\*)+')


class Deadline:
    def __init__(self, seconds=SCAN_SECONDS):
        self.end = time.monotonic() + seconds

    def left(self):
        return self.end - time.monotonic()

    @property
    def expired(self):
        return self.left() <= 0


class Scan:
    """一次扫描的状态：notes、partial、限时执行"""

    def __init__(self, deadline, clusters=()):
        self.deadline = deadline
        self.clusters = list(clusters)
        self.notes = []
        self.partial = False
        self.masker = Masker()

    def note(self, text):
        if text not in self.notes and len(self.notes) < 50:
            self.notes.append(text)

    async def run(self, coro, what=''):
        """限时执行一步：超过总时限返回 None 并标 partial；数据源报错记进 notes 返回 None"""
        left = self.deadline.left()
        if left <= 0:
            self.partial = True
            coro.close()
            return None
        try:
            return await asyncio.wait_for(coro, timeout=min(left, STEP_TIMEOUT + 5))
        except asyncio.TimeoutError:
            if self.deadline.expired:
                self.partial = True
            elif what:
                self.note(f'{what}超时，跳过')
            return None
        except LogError as exc:
            if what:
                self.note(f'{what}失败：{exc.message[:200]}')
            return None

    def sample(self, row):
        return {key: clip_value(self.masker.value(key, value), VALUE_CHARS, VALUE_CHARS) for key, value in row.items()}

    def values(self, items):
        return [clip_value(self.masker.text(str(item)), VALUE_CHARS, VALUE_CHARS) for item in items
                if item not in (None, '')]


# ---------------------------------------------------------------------------
# 规则：候选字段、名字、类型
# ---------------------------------------------------------------------------
def _matches(patterns, name):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def selected(name, include, exclude):
    if include and not _matches(include, name):
        return False
    return not (exclude and _matches(exclude, name))


def name_guess(target):
    """目标名 → 日志源 ID 候选：去掉库名、dist_ 前缀、_local 后缀、.ds- 前缀和日期，其他数字保留（例如 b2b、app2）"""
    name = _DATE_LIKE.sub('', re.sub(r'^\.ds-', '', str(target)))
    head, dot, tail = name.partition('.')
    if dot and tail and '*' not in name and '-' not in head and '.' not in tail:
        name = tail  # ClickHouse 的 库.表（ES 的名字里也可能有点，带 - 或多个点的不当成库名）
    name = re.sub(r'^dist_', '', name)
    name = re.sub(r'_local$', '', name)
    name = name.replace('*', '')
    name = re.sub(r'[^a-z0-9_-]+', '_', name.lower())
    return re.sub(r'[_-]{2,}', '_', name).strip('_-') or 'logs'


def group_name(targets):
    """组名：各目标名字相同就用它；不同（例如一个集群一张表）取共同的词，没有共同的词用第一个"""
    names = [name_guess(target['target']) for target in targets] or ['logs']
    if len(set(names)) == 1:
        return names[0]
    parts = [re.split(r'[_-]', name) for name in names]
    common = [part for part in parts[0] if part and all(part in other for other in parts[1:])]
    return '_'.join(dict.fromkeys(common)) or names[0]


def representative_first(targets):
    """行数最多的目标排到最前：样本、字段取值、字段猜测都用它（组里第一张表可能是空表）"""
    targets.sort(key=lambda target: -(target.get('rows') or 0))
    return targets[0]


def _is_time(kind):
    return kind == 'date'


def _is_text(kind):
    return kind in ('string', 'text', 'keyword')


def _is_number(kind):
    return kind in ('int', 'float', 'number')


TYPE_RULES = {'time': _is_time, 'message': _is_text, 'status': _is_number, 'duration': _is_number}


def guess_fields(columns, sorting_key='', samples=()):
    """候选字段猜测（§10.3）：columns = [(名字, kind)] → {语义: 字段名, duration_unit}"""
    by_lower = {}
    for name, kind in columns:
        by_lower.setdefault(name.lower(), (name, kind))
    guess = {}
    if sorting_key:  # 排序键里的第一个时间列
        for part in re.findall(r'[A-Za-z_@][A-Za-z0-9_.@]*', sorting_key):
            item = by_lower.get(part.lower())
            if item and _is_time(item[1]):
                guess['time'] = item[0]
                break
    for key, names in CANDIDATES.items():
        if key in guess:
            continue
        rule = TYPE_RULES.get(key)
        for candidate in names:
            item = by_lower.get(candidate.lower())
            if item and (rule is None or rule(item[1])):
                guess[key] = item[0]
                break
    if 'path' in guess and any(_LOG_FILE.search(str(row.get(guess['path']) or '')) for row in samples):
        del guess['path']  # 采集器记的日志文件路径，不是请求路径
    if 'duration' in guess:
        name = guess['duration']
        is_float = by_lower[name.lower()][1] == 'float'
        has_fraction = any(isinstance(row.get(name), float) and not float(row[name]).is_integer() for row in samples)
        guess['duration_unit'] = 's' if name.lower().endswith('time') and (is_float or has_fraction) else 'ms'
    return guess


def kind_guess(fields):
    if fields.get('status') and fields.get('path'):
        return 'access'
    if fields.get('message'):
        return 'app'
    return 'other'


def cluster_candidates(columns):
    names = {name.lower(): name for name, _ in columns}
    return [names[name] for name in CLUSTER_NAMES if name in names]


def _shape(value):
    text = str(value)
    if _UUID_RE.fullmatch(text):
        return 'uuid'
    if _HEX_RE.fullmatch(text):
        return f'hex{len(text)}'
    return ''


def id_fields(columns, samples):
    result = []
    for name, _ in columns:
        lower = name.lower()
        named = any(word in lower for word in ID_WORDS) and (lower.endswith('id') or name.endswith('Id'))
        shapes = [_shape(row.get(name)) for row in samples if row.get(name) not in (None, '')]
        shape = next((item for item in shapes if item), '')
        if named or shape:
            result.append({'field': name, 'shape': shape or 'other'})
    return result[:20]


def text_ids(messages):
    """正文里的 ID 写法：TID:xxx、"traceId":"xxx" 这类，统计出现次数"""
    counts = {}
    for message in messages:
        if not isinstance(message, str):
            continue
        for match in _TEXT_ID.finditer(message):
            key = (f'{match.group(1)}{match.group(2)}{{value}}', _shape(match.group(3)) or 'other')
            counts[key] = counts.get(key, 0) + 1
        for match in _JSON_ID.finditer(message):
            key = (f'"{match.group(1)}"{match.group(2)}"{{value}}"', _shape(match.group(3)) or 'other')
            counts[key] = counts.get(key, 0) + 1
    items = [{'text': text, 'shape': shape, 'count': count} for (text, shape), count in counts.items()
             if len(text) <= 100]
    items.sort(key=lambda item: -item['count'])
    return items[:10]


def _hash(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]


def retention_from_ttl(create_query):
    match = _TTL_DAYS.search(create_query or '')
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def duration_days(text):
    match = _DURATION.fullmatch(str(text or '').strip())
    if not match:
        return None
    number, unit = int(match.group(1)), match.group(2)
    seconds = number * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}.get(unit, 0)
    if unit == 'ms':
        seconds = number / 1000
    return round(seconds / 86400, 1) if seconds else None


# ---------------------------------------------------------------------------
# 绑定建议
# ---------------------------------------------------------------------------
def tokens(text):
    return {part for part in re.split(r'[-_.\s]+', str(text).lower()) if len(part) >= 3 and part not in GENERIC_WORDS}


RANKS = {'exact': 4, 'alias': 3, 'token': 2, 'table': 1}


def _parts(text):
    return [part for part in re.split(r'[-_.\s]+', str(text).lower()) if part]


def _is_env_word(part, word):
    return part == word or part.startswith(word) or part.endswith(word)


def name_weights(clusters):
    """集群名里每个词的区分度：出现在越多集群名里越弱（1 / 含这个词的集群数）"""
    counts = {}
    for cluster in clusters:
        names = [cluster['env'], *(cluster.get('aliases') or [])]
        for part in set().union(*(set(_parts(name)) for name in names)):
            counts[part] = counts.get(part, 0) + 1
    return {part: 1 / count for part, count in counts.items()}


def table_score(cluster, target, weights):
    """一表（一索引）一集群时按名字给集群打分：共同的词按区分度加 2 倍、包含关系（east ⊂ cneast）加 1 倍，
    通用词（prod、beta…）不加分；beta / test 这类环境词一边有一边没有扣 2 分；没有长度 ≥ 3 的非通用共同词时 0 分"""
    target_parts = _parts(target)
    best = 0.0
    for name in [cluster['env'], *(cluster.get('aliases') or [])]:
        env_parts = _parts(name)
        score, shared = 0.0, False
        for part in env_parts:
            if part in GENERIC_WORDS:  # prod、beta 这类词很多集群名都有，只在下面判断环境是否对得上
                continue
            weight = weights.get(part, 1.0)
            if part in target_parts:
                score += 2 * weight
                shared = shared or len(part) >= 3
            elif len(part) >= 3 and any(part in item for item in target_parts):
                score += weight
        for word in ENV_WORDS:
            if (any(_is_env_word(part, word) for part in env_parts)
                    != any(_is_env_word(part, word) for part in target_parts)):
                score -= 2
        if shared:
            best = max(best, score)
    return best


def _table_matches(group, clusters, weights, taken):
    """没有集群字段的组：每个目标按名字给集群打分，分高的先配；一个目标只配一个集群、一个集群只配一个目标；
    同一目标的最高分有两个集群并列（分不清）时不建议"""
    scored = {}
    for target in group['targets']:
        for cluster in clusters:
            if cluster['env'] in taken:
                continue
            score = table_score(cluster, target['target'], weights)
            if score >= MIN_TABLE_SCORE:
                scored.setdefault(target['target'], []).append((score, cluster['env']))
    candidates = []
    for target, items in scored.items():
        items.sort(key=lambda item: -item[0])
        if len(items) > 1 and abs(items[0][0] - items[1][0]) < 1e-9:
            continue
        candidates.extend((score, env, target) for score, env in items)
    candidates.sort(key=lambda item: -item[0])
    result, used = {}, set()
    for _, env, target in candidates:
        if env in result or target in used:
            continue
        used.add(target)
        result[env] = {'env': env, 'group': group['ref'], 'target': target, 'conditions': [], 'match': 'table'}
    return result


def value_owners(values, clusters, weights, known):
    """集群字段里和集群名对不上的取值（例如 shop-east-1）：按 table_score 归给得分最高的集群。
    和某个集群名 / 别名完全相同的取值只属于那个集群，不参与；分数不够或并列不归属——宁可不建议，
    也不能把别的集群的取值建议进条件（照单全收就会查到别的集群的日志）"""
    owners = {}
    for value in values:
        if value.lower() in known:
            continue
        scored = sorted(((table_score(cluster, value, weights), cluster['env']) for cluster in clusters),
                        key=lambda item: -item[0])
        if scored and scored[0][0] >= MIN_TABLE_SCORE and (len(scored) < 2 or scored[0][0] - scored[1][0] > 1e-9):
            owners[value] = scored[0][1]
    return owners


def _add_target(binding, target, picked):
    """ES 的一个绑定可以写多个索引（逗号分隔，最多 20 个）：同一组里这个集群的数据分在几个流里时都带上，
    条件的取值合并"""
    names = binding['target'].split(',')
    if target in names or len(names) >= 20:
        return
    binding['target'] = ','.join([*names, target])
    values = binding['conditions'][0]['values']
    binding['conditions'][0]['values'] = (values + [value for value in picked if value not in values])[:50]


def suggest_bindings(groups, clusters, conn_type):
    """有集群字段的组：组内每个目标 × 每个集群，字段取值和集群名 / 别名相同（不区分大小写）→ exact / alias；
    其余取值按名字打分归给唯一得分最高的集群 → token（见 value_owners）。
    没有集群字段的组（一个集群一张表 / 一个索引）按名字打分 → table（不带条件，见 table_score）。
    每个集群每组最多一条（取最强的）；Elasticsearch 同样强度的多个目标合成一条（逗号分隔的多个索引）"""
    weights = name_weights(clusters)
    known = {str(name).lower() for cluster in clusters for name in [cluster['env'], *(cluster.get('aliases') or [])]}
    result = []
    for group in groups:
        field = group.get('cluster_field_guess') or ''
        best = {}
        for target in group['targets'] if field else []:
            values = [str(value) for value in (target.get('values') or {}).get(field, [])]
            owners = value_owners(values, clusters, weights, known)
            for cluster in clusters:
                env, aliases = cluster['env'], [str(item) for item in cluster.get('aliases') or []]
                env_lower, alias_lower = env.lower(), {item.lower() for item in aliases}
                exact = [value for value in values if value.lower() == env_lower]
                alias = [value for value in values if value.lower() in alias_lower]
                if exact or alias:
                    match = 'exact' if exact else 'alias'
                    picked = exact + [item for item in alias if item not in exact]
                else:
                    picked = [value for value in values if owners.get(value) == env]
                    match = 'token' if picked else ''
                current = best.get(env)
                if not match or (current and RANKS[current['match']] > RANKS[match]):
                    continue
                if current and RANKS[current['match']] == RANKS[match]:
                    if conn_type == 'elasticsearch':
                        _add_target(current, target['target'], picked)
                    continue
                best[env] = {'env': env, 'group': group['ref'], 'target': target['target'],
                             'conditions': [{'field': field, 'op': 'in', 'values': picked[:50]}], 'match': match}
        if not field:
            best.update(_table_matches(group, clusters, weights, set(best)))
        result.extend(best.values())
    return result


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------
async def _ch_tables(scan, client, include, exclude):
    params = Params()
    sql = ('SELECT database, name, engine, engine_full, total_rows, total_bytes, partition_key, sorting_key, '
           'create_table_query, comment\nFROM system.tables\n'
           f'WHERE database NOT IN {params.add("skip", "Array(String)", list(CH_EXCLUDE))} AND NOT is_temporary'
           " AND name NOT LIKE '.inner%'\nORDER BY database, name\n"
           f'LIMIT {params.add("limit", "UInt32", CH_TABLES * 3)}')
    result = await client.query(sql, params)
    tables = []
    for row in ch_rows(result):
        name = f'{row["database"]}.{row["name"]}'
        engine = str(row.get('engine') or '')
        if not (engine.endswith('MergeTree') or engine == 'Distributed'):
            continue
        if not selected(name, include, exclude):
            continue
        tables.append(row)
        if len(tables) >= CH_TABLES:
            scan.note(f'表太多，只扫描前 {CH_TABLES} 张（用 include / exclude 缩小范围）')
            break
    return tables


async def _ch_parts(scan, client, cluster, pairs):
    """各表的行数、时间范围（活跃分区）；cluster 为空时只查本节点"""
    params = Params()
    source = f'clusterAllReplicas({quoted(cluster)}, system.parts)' if cluster else 'system.parts'
    sql = (f'SELECT database, table, sum(rows) AS rows, toUnixTimestamp(min(min_time)) AS first, '
           f'toUnixTimestamp(max(max_time)) AS last\nFROM {source}\nWHERE active AND '
           f'concat(database, \'.\', table) IN {params.add("tables", "Array(String)", pairs)}'
           '\nGROUP BY database, table')
    result = await scan.run(client.query(sql, params), '读取分区统计（system.parts）')
    if result is None:
        return {}
    stats = {}
    for row in ch_rows(result):
        first, last = to_int(row.get('first'), 0), to_int(row.get('last'), 0)
        stats[f'{row["database"]}.{row["table"]}'] = {
            'rows': to_int(row.get('rows'), 0),
            'first': iso_ms(first * 1000) if first > 86400 else None,
            'last': iso_ms(last * 1000) if last > 86400 else None,
        }
    return stats


async def _ch_columns(client, databases):
    params = Params()
    sql = ('SELECT database, table, name, type FROM system.columns\n'
           f'WHERE database IN {params.add("dbs", "Array(String)", sorted(databases))}'
           '\nORDER BY database, table, position')
    result = await client.query(sql, params)
    columns = {}
    for database, table, name, type_ in result['data']:
        columns.setdefault(f'{database}.{table}', []).append((str(name), str(type_)))
    return columns


def _time_filter(column_name, minutes):
    return f'`{column_name}` > now() - INTERVAL {int(minutes)} MINUTE' if column_name else '1'


async def _ch_topk(scan, client, target, names, time_column):
    if not names:
        return {}
    exprs = ', '.join(f'topK(10)(`{name}`) AS v{index}' for index, name in enumerate(names))
    sql = f'SELECT {exprs}\nFROM {_quote_table(target)}\nWHERE {_time_filter(time_column, 15)}'
    result = await scan.run(client.query(sql), f'取 {target} 的字段取值')
    if result is None or not result['data']:
        return {}
    row = result['data'][0]
    return {name: scan.values(row[index] or []) for index, name in enumerate(names)}


def _quote_table(target):
    database, table = target.split('.', 1)
    return f'`{database}`.`{table}`'


async def scan_clickhouse(scan, conn, session, include, exclude):
    client = ClickHouseClient(conn, session, timeout=STEP_TIMEOUT)
    version = await client.query('SELECT version() AS version, timezone() AS timezone')
    info = ch_rows(version)[0] if version['data'] else {}
    connection = {'id': conn['id'], 'type': 'clickhouse', 'version': str(info.get('version') or ''),
                  'timezone': str(info.get('timezone') or '')}
    tables = await _ch_tables(scan, client, include, exclude)
    by_name = {f'{row["database"]}.{row["name"]}': row for row in tables}
    locals_of, cluster_of = {}, {}
    for name, row in by_name.items():
        if row['engine'] != 'Distributed':
            continue
        match = _DISTRIBUTED.search(str(row.get('engine_full') or ''))
        if not match:
            continue
        cluster, database, table = match.groups()
        database = database if database and database != 'currentDatabase()' else row['database']
        locals_of[name] = f'{database}.{table}'
        if _SAFE_CLUSTER.fullmatch(cluster):
            cluster_of[name] = cluster
    hidden = set(locals_of.values())
    targets = [name for name in by_name if name not in hidden]
    columns = await scan.run(_ch_columns(client, {row['database'] for row in tables}), '读取字段（system.columns）') \
        or {}
    stats = {}
    for cluster in sorted(set(cluster_of.values())):
        names = [locals_of[name] for name in targets if cluster_of.get(name) == cluster]
        found = await _ch_parts(scan, client, cluster, names)
        for name in targets:
            if cluster_of.get(name) == cluster and locals_of[name] in found:
                stats[name] = found[locals_of[name]]
    plain = [name for name in targets if name not in locals_of]
    if plain:
        stats.update(await _ch_parts(scan, client, '', plain))
    groups = {}
    for name in targets:
        row = by_name[name]
        cols = columns.get(name) or columns.get(locals_of.get(name, ''), [])
        if not cols:
            continue
        local_row = by_name.get(locals_of.get(name, ''), row)
        target = {
            'target': name, 'local': locals_of.get(name, ''), 'cluster': cluster_of.get(name, ''),
            'rows': stats.get(name, {}).get('rows', to_int(local_row.get('total_rows') or row.get('total_rows'), 0)),
            'bytes': to_int(local_row.get('total_bytes') or row.get('total_bytes'), 0),
            'first': stats.get(name, {}).get('first'), 'last': stats.get(name, {}).get('last'),
            'retention_days': retention_from_ttl(local_row.get('create_table_query')),
            'sorting_key': str(local_row.get('sorting_key') or ''),
            'partition_key': str(local_row.get('partition_key') or ''),
            'values': {}, 'samples': [],
        }
        key = _hash([f'{col}:{type_}' for col, type_ in cols])
        group = groups.setdefault(key, {'columns': cols, 'targets': [], 'comment': ''})
        group['targets'].append(target)
        group['comment'] = group['comment'] or str(local_row.get('comment') or row.get('comment') or '')
    result_groups = []
    for index, group in enumerate(split_by_table(list(groups.values())), start=1):
        cols = group['columns']
        kinds = [(name, kind_of(type_)) for name, type_ in cols]
        first = representative_first(group['targets'])
        fields = guess_fields(kinds, first['sorting_key'])
        time_column = fields.get('time', '')
        sample_sql = f'SELECT *\nFROM {_quote_table(first["target"])}\nWHERE {_time_filter(time_column, 10)}\nLIMIT 2'
        sampled = await scan.run(client.query(sample_sql), f'取 {first["target"]} 的样本')
        samples = ch_rows(sampled) if sampled else []
        fields = guess_fields(kinds, first['sorting_key'], samples)
        candidates = cluster_candidates(kinds)
        low = [name for name, type_ in cols if ('LowCardinality' in type_ or 'Enum' in type_)
               and name not in candidates]
        names = (candidates + low)[:TOPK_FIELDS]
        first['values'] = await _ch_topk(scan, client, first['target'], names, time_column)
        first['samples'] = [scan.sample(row) for row in samples]
        for target in group['targets'][1:OTHER_TARGETS + 1]:
            if candidates:
                target['values'] = await _ch_topk(scan, client, target['target'], candidates[:1], time_column)
        cluster_field = _cluster_field(candidates, group['targets'], scan.clusters)
        messages = [row.get(fields['message']) for row in samples] if fields.get('message') else []
        result_groups.append({
            'ref': f'g{index}', 'name_guess': group_name(group['targets']), 'kind_guess': kind_guess(fields),
            'columns': [{'name': name, 'type': type_} for name, type_ in cols[:COLUMNS]],
            'fields_guess': fields, 'cluster_field_guess': cluster_field,
            'id_fields': id_fields(kinds, samples), 'text_ids': text_ids(messages),
            'meta_description': group['comment'][:2000], 'targets': group['targets'],
        })
        if scan.deadline.expired:
            scan.partial = True
            break
    return connection, result_groups


def split_by_table(groups):
    """字段完全相同、但每个表名都在多个库里出现（例如每个环境一个库，库里各有 gateway、web 两种访问日志表）：
    按表名拆开，每种表各是一类日志。一个集群一张表（表名各不相同）的不拆"""
    result = []
    for group in groups:
        bases = {}
        for target in group['targets']:
            database, _, table = target['target'].partition('.')
            bases.setdefault(table, set()).add(database)
        if len(bases) >= 2 and all(len(databases) >= 2 for databases in bases.values()):
            for table in bases:
                result.append({**group, 'targets': [target for target in group['targets']
                                                    if target['target'].partition('.')[2] == table]})
        else:
            result.append(group)
    return result


def _cluster_field(candidates, targets, clusters):
    """候选集群字段：名字匹配的优先；否则 topK 里能对上集群名 / 别名的低基数字段"""
    if candidates:
        return candidates[0]
    names = set()
    for cluster in clusters:
        names.add(cluster['env'].lower())
        names.update(str(item).lower() for item in cluster.get('aliases') or [])
    for target in targets:
        for field, values in (target.get('values') or {}).items():
            if any(str(value).lower() in names for value in values):
                return field
    return ''


# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------
def pattern_of(index):
    """独立索引按名字把数字串换成 * 归成模式（nginx-2026.11.23 → nginx-*）"""
    return _STAR_RUN.sub('*', _DIGITS.sub('*', index))


async def _es_targets(scan, client, include, exclude):
    targets = {}
    resolved = await scan.run(client.call('GET', '/_resolve/index/*', {'expand_wildcards': 'open'}), '列出索引')
    if resolved is not None:
        for item in resolved.get('data_streams') or []:
            targets[item['name']] = {'kind': 'data_stream', 'indices': item.get('backing_indices') or []}
        for item in resolved.get('aliases') or []:
            targets.setdefault(item['name'], {'kind': 'alias', 'indices': item.get('indices') or []})
        for item in resolved.get('indices') or []:
            if item.get('data_stream') or item.get('aliases'):
                continue
            name = pattern_of(item['name'])
            targets.setdefault(name, {'kind': 'pattern' if name != item['name'] else 'index', 'indices': []})
            targets[name]['indices'].append(item['name'])
    else:
        cat_params = {'format': 'json', 'h': 'index,docs.count,store.size'}
        listed = await scan.run(client.call('GET', '/_cat/indices', cat_params),
                                '列出索引（_cat/indices）')
        for item in listed or []:
            name = pattern_of(str(item.get('index') or ''))
            if name:
                targets.setdefault(name, {'kind': 'pattern', 'indices': []})['indices'].append(item.get('index'))
    names = [name for name in sorted(targets) if not name.startswith('.') and selected(name, include, exclude)]
    if len(names) > ES_TARGETS:
        scan.note(f'目标太多，只扫描前 {ES_TARGETS} 个（用 include / exclude 缩小范围）')
        names = names[:ES_TARGETS]
    return {name: targets[name] for name in names}


async def _es_templates(scan, client):
    index_templates = await scan.run(client.call('GET', '/_index_template'), '读取索引模板（没有权限可忽略）')
    components = await scan.run(client.call('GET', '/_component_template'), '读取组件模板（没有权限可忽略）')
    component_meta = {}
    for item in (components or {}).get('component_templates') or []:
        meta = (item.get('component_template') or {}).get('_meta') or {}
        if isinstance(meta.get('description'), str):
            component_meta[item.get('name')] = meta['description']
    templates = []
    for item in (index_templates or {}).get('index_templates') or []:
        body = item.get('index_template') or {}
        meta = body.get('_meta') or {}
        description = meta.get('description') if isinstance(meta.get('description'), str) else ''
        if not description:
            description = next((component_meta[name] for name in body.get('composed_of') or []
                                if name in component_meta), '')
        templates.append((to_int(body.get('priority'), 0), body.get('index_patterns') or [], description))
    templates.sort(key=lambda item: -item[0])
    return templates


def _template_description(templates, name):
    for _, patterns, description in templates:
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
            return description
    return ''


async def _es_retention(scan, client, name, kind, policies):
    if kind != 'data_stream':
        return None
    info = await scan.run(client.call('GET', f'/_data_stream/{target_path(name)}'), '读取 data stream（没有权限可忽略）')
    stream = next(iter((info or {}).get('data_streams') or []), {})
    lifecycle = stream.get('lifecycle') or {}
    if lifecycle.get('data_retention'):
        return duration_days(lifecycle['data_retention'])
    policy = stream.get('ilm_policy')
    if not policy:
        return None
    if policy not in policies:
        data = await scan.run(client.call('GET', f'/_ilm/policy/{target_path(policy)}'), '读取 ILM 策略（没有权限可忽略）')
        phases = (((data or {}).get(policy) or {}).get('policy') or {}).get('phases') or {}
        policies[policy] = duration_days((phases.get('delete') or {}).get('min_age'))
    return policies[policy]


async def _es_time_range(scan, client, name, time_field):
    if not time_field:
        return None, None
    body = {'size': 0, 'aggs': {'first': {'min': {'field': time_field}}, 'last': {'max': {'field': time_field}}}}
    data = await scan.run(client.search(name, body), f'取 {name} 的时间范围')
    aggs = (data or {}).get('aggregations') or {}
    first, last = (aggs.get('first') or {}).get('value'), (aggs.get('last') or {}).get('value')
    return (iso_ms(first) if first else None), (iso_ms(last) if last else None)


async def _es_terms(scan, client, name, fields, time_field, schema):
    if not fields:
        return {}
    aggs = {}
    for index, field in enumerate(fields):
        info = schema.get(field) or {}
        agg_field = info.get('keyword') if info.get('kind') == 'text' else field
        if agg_field:
            aggs[f'v{index}'] = {'terms': {'field': agg_field, 'size': 10}}
    if not aggs:
        return {}
    query = {'range': {time_field: {'gte': 'now-15m'}}} if time_field else {'match_all': {}}
    data = await scan.run(client.search(name, {'size': 0, 'query': query, 'aggs': aggs}), f'取 {name} 的字段取值')
    result = {}
    for index, field in enumerate(fields):
        buckets = (((data or {}).get('aggregations') or {}).get(f'v{index}') or {}).get('buckets') or []
        if buckets:
            result[field] = scan.values([bucket.get('key_as_string', bucket.get('key')) for bucket in buckets])
    return result


async def _es_docs(scan, client, name, time_field, size, minutes=10, source=None):
    body = {'size': size, 'query': {'range': {time_field: {'gte': f'now-{minutes}m'}}} if time_field
            else {'match_all': {}}}
    if time_field:
        body['sort'] = [{time_field: {'order': 'desc', 'unmapped_type': 'date'}}]
    if source is not None:
        body['_source'] = source
    data = await scan.run(client.search(name, body), f'取 {name} 的样本')
    return [flatten(hit.get('_source') or {}) for hit in ((data or {}).get('hits') or {}).get('hits') or []]


def _es_column(name, type_, schema):
    column = {'name': name, 'type': type_}
    if (schema.get(name) or {}).get('runtime'):
        column['runtime'] = True  # 运行时字段：没有索引，按它过滤要逐条计算
    return column


def _mark_runtime_ids(items, schema):
    """ID 字段标出运行时字段并排到后面：按它关联要逐条计算，同一个 ID 在正文里有写法时优先用正文写法"""
    for item in items:
        if (schema.get(item['field']) or {}).get('runtime'):
            item['runtime'] = True
    return sorted(items, key=lambda item: bool(item.get('runtime')))


async def scan_elastic(scan, conn, session, include, exclude):
    client = ElasticClient(conn, session, timeout=STEP_TIMEOUT)
    info = describe_info(await client.call('GET', '/'))
    connection = {'id': conn['id'], 'type': 'elasticsearch', 'version': info['version'],
                  'distribution': info['distribution']}
    targets = await _es_targets(scan, client, include, exclude)
    templates = await _es_templates(scan, client)
    if not templates:
        scan.note('没有读到索引模板（没有权限或没有模板），跳过模板说明')
    policies, groups = {}, {}
    for name, meta in targets.items():
        if scan.deadline.expired:
            scan.partial = True
            break
        caps = await scan.run(client.field_caps(name), f'读取 {name} 的字段')
        schema = field_schema.parse_field_caps(caps or {})
        if not schema:
            continue
        cols = [(field, info['type']) for field, info in schema.items()]
        kinds = [(field, info['kind']) for field, info in schema.items()]
        fields = guess_fields(kinds)
        count = await scan.run(client.count(name, {'match_all': {}}), f'统计 {name} 的条数')
        first, last = await _es_time_range(scan, client, name, fields.get('time'))
        target = {
            'target': name, 'kind': meta['kind'], 'rows': count, 'bytes': None, 'first': first, 'last': last,
            'retention_days': await _es_retention(scan, client, name, meta['kind'], policies),
            'values': {}, 'samples': [],
        }
        key = _hash(sorted(schema))
        group = groups.setdefault(key, {'columns': cols, 'kinds': kinds, 'schema': schema, 'targets': [],
                                        'description': ''})
        group['targets'].append(target)
        group['description'] = group['description'] or _template_description(templates, name)
    result_groups = []
    for index, group in enumerate(groups.values(), start=1):
        if scan.deadline.expired:
            scan.partial = True
            break
        first = representative_first(group['targets'])
        await scan.run(field_schema.mark_runtime(group['schema'], client, first['target']), f'读取 {first["target"]} 的映射')
        fields = guess_fields(group['kinds'])
        time_field = fields.get('time', '')
        samples = await _es_docs(scan, client, first['target'], time_field, 2)
        fields = guess_fields(group['kinds'], '', samples)
        candidates = cluster_candidates(group['kinds'])
        others = [fields[key] for key in ('level', 'namespace', 'service', 'method', 'host', 'status')
                  if fields.get(key) and fields[key] not in candidates]
        names = (candidates + others)[:TOPK_FIELDS]
        first['values'] = await _es_terms(scan, client, first['target'], names, time_field, group['schema'])
        first['samples'] = [scan.sample(row) for row in samples]
        for target in group['targets'][1:OTHER_TARGETS + 1]:
            if candidates:
                target['values'] = await _es_terms(scan, client, target['target'], candidates[:1], time_field,
                                                   group['schema'])
        messages = []
        if fields.get('message'):
            extra = await _es_docs(scan, client, first['target'], time_field, TEXT_ID_SAMPLES, 60,
                                   [fields['message']])
            messages = [row.get(fields['message']) for row in samples + extra]
        result_groups.append({
            'ref': f'g{index}', 'name_guess': group_name(group['targets']), 'kind_guess': kind_guess(fields),
            'columns': [_es_column(name, type_, group['schema']) for name, type_ in group['columns'][:COLUMNS]],
            'fields_guess': fields, 'cluster_field_guess': _cluster_field(candidates, group['targets'], scan.clusters),
            'id_fields': _mark_runtime_ids(id_fields(group['kinds'], samples), group['schema']),
            'text_ids': text_ids(messages),
            'meta_description': group['description'][:2000], 'targets': group['targets'],
        })
    return connection, result_groups


async def run(conn, session, include, exclude, clusters, seconds=SCAN_SECONDS):
    """扫描一个连接 → §10.4 的结果"""
    started = time.monotonic()
    scan = Scan(Deadline(seconds), clusters)
    if conn['type'] == 'clickhouse':
        connection, groups = await scan_clickhouse(scan, conn, session, include, exclude)
    else:
        connection, groups = await scan_elastic(scan, conn, session, include, exclude)
    return {
        'connection': connection, 'partial': scan.partial, 'elapsed_ms': int((time.monotonic() - started) * 1000),
        'notes': scan.notes, 'groups': groups,
        'suggested_bindings': suggest_bindings(groups, clusters, conn['type']),
        'clusters': clusters,
    }
