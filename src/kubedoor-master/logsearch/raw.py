"""原生查询（约定 §6）的校验与包装：ClickHouse SQL、Elasticsearch DSL、ES|QL。

ClickHouse SQL（§6.1，占位符 + AST 两步）：
1. 词法：跳过 '…'（\\ 转义和 ''）、"…"、`…`、$tag$…$tag$ 和 --、#、/* */（可嵌套）注释；末尾的 ; 去掉，其余位置
   出现 ; 拒绝；代码里要有 $src（词边界）；原文出现 __kd_src__ 拒绝；$src 前面紧挨着的词只能是 FROM / JOIN
   等关键字（否则替换成「(子查询)」后会变成函数调用，例如 file $src → file((SELECT …))）
2. 每个 $src 换成裸标识符 __kd_src__，对这份文本 EXPLAIN AST，检查：根是 SelectWithUnionQuery 且只有 1 个子节点；
   没有 Set 节点；TableExpression 下没有 Function（表函数）；每个 TableIdentifier 正好是 __kd_src__（直接写表名、
   表 CTE 一律拒绝）；in 系列函数的第二个参数不是 Identifier；没有黑名单函数
3. 通过后在同一批位置把 $src 换成 (SELECT * FROM `db`.`t` WHERE <时间> AND <绑定条件>)（单行、参数绑定）再执行
Elasticsearch DSL（§6.2）：只允许 query aggs aggregations sort _source size track_total_hits；树里出现 script、
global、_script、significant_terms 等键拒绝，terms / multi_terms 聚合的 min_doc_count 不能是 0（这两类会带出
集群过滤之外的取值或计数）；外面套一层 bool.filter（时间 + 绑定条件）
ES|QL（§6.3）：词法正确处理 \"\"\"…\"\"\" 和带 \\ 转义的 "…"；按引号外的 | 切分，每段第一个词在白名单里；
去掉字符串后出现 LOOKUP / ENRICH / FORK / FROM / ROW / SHOW / METADATA / JOIN 整词拒绝；不允许注释
"""

import json
import re

from .common import LogError, clip_value, to_int

PLACEHOLDER = '__kd_src__'
MAX_SQL = 20000
MAX_DSL = 50000
MAX_ESQL = 20000
MAX_DEPTH = 64
MAX_TRACK_TOTAL = 10000
MAX_RESULT_BYTES = 16 * 1024 * 1024
IN_FUNCTIONS = frozenset(name.lower() for name in (
    'in', 'notIn', 'globalIn', 'globalNotIn', 'nullIn', 'notNullIn', 'globalNullIn', 'globalNotNullIn'))
DENIED_FUNCTIONS = tuple(name.lower() for name in (
    'dict', 'joinGet', 'file', 'url', 's3', 'hdfs', 'azure', 'gcs', 'mysql', 'postgresql', 'mongo', 'redis',
    'sqlite', 'odbc', 'jdbc', 'remote', 'cluster', 'catboost', 'getMacro', 'getSetting', 'getServerSetting',
    'hasColumnInTable', 'getClientHTTPHeader', 'filesystem', 'globalVariable', 'executable', 'logTrace',
    'currentProfiles', 'currentRoles', 'enabledRoles', 'defaultRoles', 'iceberg', 'deltaLake', 'hudi'))
# $src 前面可以紧跟的词（SQL 关键字）。其他词（函数名、列名）后面紧跟 $src，替换成「(子查询)」后会变成函数调用
SRC_PREFIX_WORDS = frozenset({'FROM', 'JOIN', 'IN', 'EXISTS', 'AND', 'OR', 'NOT', 'WHERE', 'PREWHERE', 'HAVING', 'ON',
                              'SELECT', 'THEN', 'ELSE', 'WHEN', 'ALL', 'ANY', 'DISTINCT'})
DSL_KEYS = ('query', 'aggs', 'aggregations', 'sort', '_source', 'size', 'track_total_hits')
DSL_FORBIDDEN = frozenset({'script', 'scripted_metric', 'script_score', 'runtime_mappings', 'script_fields',
                           'bucket_script', 'bucket_selector', 'percolate', 'more_like_this', 'indexed_shape',
                           'global', '_script',
                           # 背景集默认是整个索引（不受 query 限制），会带出其他集群的计数
                           'significant_terms', 'significant_text', 'background_filter'})
ESQL_COMMANDS = frozenset({'WHERE', 'EVAL', 'STATS', 'INLINESTATS', 'SORT', 'LIMIT', 'KEEP', 'DROP', 'RENAME',
                           'DISSECT', 'GROK', 'MV_EXPAND', 'SAMPLE', 'CHANGE_POINT'})
ESQL_FORBIDDEN = re.compile(r'\b(LOOKUP|ENRICH|FORK|FROM|ROW|SHOW|METADATA|JOIN)\b', re.IGNORECASE)
_WORD_START = re.compile(r'[A-Za-z_\u0080-\uffff]')
_WORD_CHAR = re.compile(r'[A-Za-z0-9_$\u0080-\uffff]')
_HEREDOC = re.compile(r'\$([A-Za-z0-9_]*)\$')
_NUMBER_CHAR = re.compile(r'[0-9A-Za-z_.]')
SRC_HINT = '只能用 $src 引用日志源（$src 已经按集群和时间过滤），例如 SELECT status, count() FROM $src GROUP BY status'
CTE_HINT = '请把 WITH x AS (子查询) 改成 FROM (子查询)，例如 SELECT … FROM (SELECT … FROM $src WHERE …) AS x'


def raw_invalid(message):
    return LogError(400, message, 'raw_invalid')


# ---------------------------------------------------------------------------
# ClickHouse：词法
# ---------------------------------------------------------------------------
def _skip_quoted(sql, start, quote):
    """引号内容：\\ 转义下一个字符，连续两个引号是一个引号；返回结束引号之后的位置"""
    i, n = start + 1, len(sql)
    while i < n:
        ch = sql[i]
        if ch == '\\':
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise raw_invalid('引号没有结束（字符串或带引号的名字）')


def _skip_block_comment(sql, start):
    depth, i, n = 1, start + 2, len(sql)
    while i < n:
        if sql.startswith('/*', i):
            depth += 1
            i += 2
        elif sql.startswith('*/', i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise raw_invalid('注释 /* 没有结束')


def lex_sql(sql):
    """→ (tokens, $src 的位置列表)；token 是 (类型, 开始, 结束)：word / qident / string / number / punct / src / semicolon"""
    tokens, spans = [], []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith('--', i) or ch == '#':
            end = sql.find('\n', i)
            i = n if end < 0 else end + 1
        elif sql.startswith('/*', i):
            i = _skip_block_comment(sql, i)
        elif ch == "'":
            end = _skip_quoted(sql, i, ch)
            tokens.append(('string', i, end))
            i = end
        elif ch in '"`':
            end = _skip_quoted(sql, i, ch)
            tokens.append(('qident', i, end))
            i = end
        elif ch == '$':
            heredoc = _HEREDOC.match(sql, i)
            if heredoc:
                close = sql.find(heredoc.group(0), heredoc.end())
                if close < 0:
                    raise raw_invalid(f'字符串 {heredoc.group(0)} 没有结束')
                end = close + len(heredoc.group(0))
                tokens.append(('string', i, end))
                i = end
            elif sql.startswith('$src', i) and not (i + 4 < n and _WORD_CHAR.match(sql[i + 4])):
                tokens.append(('src', i, i + 4))
                spans.append((i, i + 4))
                i += 4
            else:
                tokens.append(('punct', i, i + 1))
                i += 1
        elif _WORD_START.match(ch):
            j = i + 1
            while j < n and _WORD_CHAR.match(sql[j]):
                j += 1
            tokens.append(('word', i, j))
            i = j
        elif ch.isdigit():
            j = i + 1
            while j < n and _NUMBER_CHAR.match(sql[j]):
                j += 1
            tokens.append(('number', i, j))
            i = j
        elif ch == ';':
            tokens.append(('semicolon', i, i + 1))
            i += 1
        else:
            tokens.append(('punct', i, i + 1))
            i += 1
    return tokens, spans


def prepare_sql(sql):
    """词法检查 → (去掉末尾 ; 的 SQL, $src 位置列表)"""
    if not isinstance(sql, str) or not sql.strip():
        raise raw_invalid('sql 要是一条 SELECT 语句')
    if len(sql) > MAX_SQL:
        raise raw_invalid(f'sql 最长 {MAX_SQL} 个字符')
    if PLACEHOLDER in sql.lower():
        raise raw_invalid(f'sql 里不能出现 {PLACEHOLDER}（系统保留的名字）')
    tokens, spans = lex_sql(sql)
    semicolons = [index for index, token in enumerate(tokens) if token[0] == 'semicolon']
    if semicolons:
        if semicolons != [len(tokens) - 1]:
            raise raw_invalid('只能写一条语句（不能有多个 ;）')
        sql = sql[:tokens[-1][1]]
        tokens = tokens[:-1]
    if not spans:
        raise raw_invalid(f'sql 里没有 $src。{SRC_HINT}')
    for index, token in enumerate(tokens):
        if token[0] != 'src' or index == 0:
            continue
        kind, start, end = tokens[index - 1]
        if kind == 'qident' or (kind == 'word' and sql[start:end].upper() not in SRC_PREFIX_WORDS):
            raise raw_invalid(f'$src 前面不能紧跟 {sql[start:end][:40]}：$src 要写在 FROM / JOIN 等关键字后面'
                              '（或括号、逗号后面），例如 SELECT … FROM $src')
    return sql, spans


def substitute(sql, spans, replacement):
    """在同一批位置把 $src 换成 replacement"""
    parts, last = [], 0
    for start, end in spans:
        parts.append(sql[last:start])
        parts.append(replacement)
        last = end
    parts.append(sql[last:])
    return ''.join(parts)


# ---------------------------------------------------------------------------
# ClickHouse：EXPLAIN AST
# ---------------------------------------------------------------------------
class Node:
    __slots__ = ('kind', 'name', 'alias', 'children')

    def __init__(self, kind, name='', alias=None):
        self.kind, self.name, self.alias, self.children = kind, name, alias, []

    def walk(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


_CHILDREN = re.compile(r'\s*\(children \d+\)\s*$')
_ALIAS = re.compile(r'\s+\(alias ([^()]*)\)\s*$')


def parse_node(line):
    text = line.strip()
    kind, _, rest = text.partition(' ')
    rest = _CHILDREN.sub('', ' ' + rest)
    alias = None
    if kind in ('TableIdentifier', 'Function', 'Subquery', 'Identifier'):
        match = _ALIAS.search(rest)
        if match:
            alias = match.group(1)
            rest = rest[:match.start()]
    return Node(kind, rest.strip(), alias)


def parse_ast(lines):
    """EXPLAIN AST 的输出（按缩进表示层级）→ 根节点；有多个根时拒绝"""
    root, stack = None, []
    for line in lines:
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip(' '))
        node = parse_node(line)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if not stack:
            if root is not None:
                raise raw_invalid('只能写一条 SELECT 语句')
            root = node
        else:
            stack[-1][1].children.append(node)
        stack.append((depth, node))
    return root


def check_ast(root):
    """§6.1 的 AST 规则，违反任一条抛 raw_invalid"""
    if root is None or root.kind != 'SelectWithUnionQuery':
        raise raw_invalid(f'只能写一条 SELECT 查询（不能写 SHOW、DESCRIBE、INSERT 等）。{SRC_HINT}')
    if len(root.children) != 1:
        raise raw_invalid('不能写 FORMAT、INTO OUTFILE（结果格式由系统决定）')
    nodes = list(root.walk())
    has_with = any(node.kind == 'WithElement' for node in nodes)
    for node in nodes:
        if node.kind == 'Set':
            raise raw_invalid('不能写 SETTINGS（查询设置由系统决定）')
        if node.kind == 'TableExpression':
            for child in node.children:
                if child.kind == 'Function':
                    raise raw_invalid(f'不能用表函数 {child.name}（url、remote、file、numbers 等一律不行）。{SRC_HINT}')
        elif node.kind == 'TableIdentifier' and node.name != PLACEHOLDER:
            name = node.name.replace(PLACEHOLDER, '$src')
            if '.' in name:
                raise raw_invalid(f'不能直接查表 {name}。{SRC_HINT}')
            if has_with:
                raise raw_invalid(f'不支持 WITH 定义的表子查询（{name}）：{CTE_HINT}')
            raise raw_invalid(f'不能查表 {name}。{SRC_HINT}；WITH 定义的表子查询也不支持，{CTE_HINT}')
        elif node.kind == 'Function':
            lower = node.name.lower()
            if lower in IN_FUNCTIONS:
                args = next((child for child in node.children if child.kind == 'ExpressionList'), None)
                if args is not None and len(args.children) >= 2 and args.children[1].kind == 'Identifier':
                    raise raw_invalid('IN 后面不能直接写表名或名字：要写成 IN (SELECT 列 FROM $src WHERE …) 或值列表')
            if lower.startswith(DENIED_FUNCTIONS):
                raise raw_invalid(f'不能用函数 {node.name}（会读取日志源以外的数据或服务器信息）')


def wrapper_sql(plan, start_ms, end_ms):
    """$src 的替换内容（单行；时间和集群条件参数绑定）"""
    conds = plan.time_conds(start_ms, end_ms) + plan.binding_conds()
    return f'(SELECT * FROM {plan.table} WHERE {" AND ".join(conds)})'


async def run_sql(client, plan, sql, start_ms, end_ms, limit):
    """校验 → 执行；返回 (结果, 实际执行的 SQL)"""
    sql, spans = prepare_sql(sql)
    placeholder_sql = substitute(sql, spans, PLACEHOLDER)
    try:
        lines = await client.explain_ast(placeholder_sql)
    except LogError as exc:
        if exc.code == 'query_rejected':
            raise LogError(400, exc.message.replace(PLACEHOLDER, '$src'), 'query_rejected') from None
        raise
    check_ast(parse_ast(lines))
    final = substitute(sql, spans, wrapper_sql(plan, start_ms, end_ms))
    result = await client.query(final, plan.params, extra_settings={
        'max_result_rows': limit, 'result_overflow_mode': 'break', 'max_result_bytes': MAX_RESULT_BYTES})
    return result, plan.params.render(final)


# ---------------------------------------------------------------------------
# Elasticsearch DSL
# ---------------------------------------------------------------------------
def _walk_dsl(value, depth=0):
    if depth > MAX_DEPTH:
        raise raw_invalid('dsl 嵌套太深')
    if isinstance(value, dict):
        for key, item in value.items():
            if key in DSL_FORBIDDEN:
                raise raw_invalid(f'dsl 里不能用 {key}（脚本、global 聚合、跨索引查询等会绕过限制）')
            if key == 'terms' and isinstance(item, dict) and any(
                    isinstance(entry, dict) and 'index' in entry for entry in item.values()):
                raise raw_invalid('dsl 里不能用 terms lookup（从其他索引取值）')
            if key in ('terms', 'multi_terms') and isinstance(item, dict) and item.get('min_doc_count') is not None \
                    and to_int(item.get('min_doc_count'), 1) == 0:
                # 词项聚合的 min_doc_count=0 会把整个索引里没命中的取值也列出来（含其他集群的）
                raise raw_invalid(f'dsl 里 {key} 聚合的 min_doc_count 不能是 0')
            _walk_dsl(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _walk_dsl(item, depth + 1)


def check_dsl(dsl, limit):
    """校验 AI 的 DSL → 规范化后的副本（size ≤ limit，track_total_hits ≤ 10000）"""
    if not isinstance(dsl, dict):
        raise raw_invalid('dsl 要是 JSON 对象：{query, aggs, sort, _source, size}')
    unknown = [str(key) for key in dsl if key not in DSL_KEYS]
    if unknown:
        raise raw_invalid(f'dsl 只能有这些键：{" ".join(DSL_KEYS)}（不支持 {"、".join(unknown[:5])}）')
    try:
        text = json.dumps(dsl, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        raise raw_invalid('dsl 不是合法的 JSON') from None
    if len(text) > MAX_DSL:
        raise raw_invalid(f'dsl 序列化后最长 {MAX_DSL} 个字符')
    _walk_dsl(dsl)
    result = dict(dsl)
    if 'aggregations' in result and 'aggs' in result:
        raise raw_invalid('aggs 和 aggregations 只能写一个')
    size = result.get('size', limit)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise raw_invalid('dsl.size 要是非负整数')
    result['size'] = min(size, limit)
    track = result.get('track_total_hits', MAX_TRACK_TOTAL)
    if track is True:
        track = MAX_TRACK_TOTAL
    elif isinstance(track, bool):
        track = False
    elif isinstance(track, int) and track >= 0:
        track = min(track, MAX_TRACK_TOTAL)
    else:
        raise raw_invalid('dsl.track_total_hits 要是 true / false 或整数')
    result['track_total_hits'] = track
    if 'query' in result and not isinstance(result['query'], dict):
        raise raw_invalid('dsl.query 要是对象')
    return result


def wrap_dsl(dsl, base_filter):
    """外面套一层：bool.filter = 时间 + 集群绑定条件，bool.must = AI 的 query（没有就 match_all）"""
    outer = json.loads(json.dumps(base_filter))
    outer['bool']['must'] = [dsl.get('query') or {'match_all': {}}]
    return {**{key: value for key, value in dsl.items() if key != 'query'}, 'query': outer}


def mask_tree(masker, value, key=''):
    """聚合结果等嵌套结构：按键名整项打码、字符串文本打码并截断"""
    if isinstance(value, dict):
        return {name: ('***' if masker.hidden(name) and item not in (None, '') else mask_tree(masker, item, name))
                for name, item in value.items()}
    if isinstance(value, list):
        return [mask_tree(masker, item, key) for item in value]
    if isinstance(value, str):
        return clip_value(masker.text(value), 300, 300)
    return value


def trim_json(value, limit):
    """序列化超过 limit 字时，从尾部砍掉列表元素（桶），返回 (结果, 是否截断)"""
    def size(item):
        return len(json.dumps(item, ensure_ascii=False, default=str))

    if size(value) <= limit:
        return value, False
    lists = []

    def collect(item):
        if isinstance(item, dict):
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            lists.append(item)
            for child in item:
                collect(child)

    collect(value)
    lists.sort(key=len, reverse=True)
    for items in lists:
        while len(items) > 1 and size(value) > limit:
            del items[max(1, len(items) // 2):]
        if size(value) <= limit:
            break
    return value, True


# ---------------------------------------------------------------------------
# ES|QL
# ---------------------------------------------------------------------------
def _esql_string(text, i):
    """普通字符串 "…"：\\ 转义下一个字符，不能跨行；返回结束引号之后的位置"""
    j, n = i + 1, len(text)
    while j < n:
        ch = text[j]
        if ch == '\\':
            j += 2
            continue
        if ch in '\r\n':
            raise raw_invalid('ES|QL 的字符串不能跨行')
        if ch == '"':
            return j + 1
        j += 1
    raise raw_invalid('ES|QL 的字符串没有结束')


def lex_esql(text):
    """→ (各段文本, 去掉字符串和带引号名字后的文本)"""
    stripped, i, n = [], 0, len(text)
    while i < n:
        if text.startswith('"""', i):
            close = text.find('"""', i + 3)
            if close < 0 or '\n' in text[i:close] or '\r' in text[i:close]:
                raise raw_invalid('ES|QL 的三引号字符串没有结束或跨行了')
            j = close + 3
            for _ in range(2):  # 结束的 """ 后面最多再跟两个 "（算字符串内容）
                if j < n and text[j] == '"':
                    j += 1
            stripped.append(' ')
            i = j
        elif text[i] == '"':
            i = _esql_string(text, i)
            stripped.append(' ')
        elif text[i] == '`':
            j = i + 1
            while True:
                close = text.find('`', j)
                if close < 0:
                    raise raw_invalid('ES|QL 的反引号没有结束')
                if text.startswith('``', close):
                    j = close + 2
                    continue
                break
            stripped.append(' ')
            i = close + 1
        elif text.startswith('//', i) or text.startswith('/*', i):
            raise raw_invalid('ES|QL 里不要写注释（// 或 /* */）')
        else:
            stripped.append(text[i])
            i += 1
    clean = ''.join(stripped)
    return clean.split('|'), clean


def check_esql(esql):
    """校验 FROM 之后的管道 → 规范化的管道（不带开头的 |）"""
    if not isinstance(esql, str) or not esql.strip():
        raise raw_invalid('esql 要是 FROM 之后的管道，例如 | STATS c = COUNT(*) BY level')
    if len(esql) > MAX_ESQL:
        raise raw_invalid(f'esql 最长 {MAX_ESQL} 个字符')
    text = esql.strip()
    segments, clean = lex_esql(text)
    if segments and not segments[0].strip():
        segments = segments[1:]
        text = text[text.index('|') + 1:].strip()
    if not segments:
        raise raw_invalid('esql 是空的')
    for segment in segments:
        words = segment.split()
        if not words:
            raise raw_invalid('esql 里有空的管道段（连续的 |）')
        if words[0].upper() not in ESQL_COMMANDS:
            raise raw_invalid(f'esql 不能用命令 {words[0][:30]}：只能用 {" ".join(sorted(ESQL_COMMANDS))}'
                              '（FROM 由系统按日志源生成）')
    found = ESQL_FORBIDDEN.search(clean)
    if found:
        raise raw_invalid(f'esql 里不能出现 {found.group(1).upper()}（只能查当前日志源）')
    return text


def esql_query(target, pipeline, limit):
    sources = ', '.join(item.strip() for item in target.split(',') if item.strip())
    return f'FROM {sources} | {pipeline} | LIMIT {int(limit)}'


def result_rows(result, limit):
    """ClickHouse JSONCompact → (列名, 行)，按 limit 截"""
    columns = [str(item.get('name')) for item in result.get('meta') or []]
    rows = result.get('data') or []
    return columns, rows[:limit], len(rows) > limit or (to_int((result.get('rows_before_limit_at_least') or 0))
                                                         > len(rows[:limit]))
