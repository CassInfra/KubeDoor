"""原生查询校验（logsearch/raw.py）：词法、占位符 + EXPLAIN AST 的每条规则（用 ClickHouse 26.7 生成的样本）、
包装后的 SQL 一定带集群过滤、Elasticsearch DSL 和 ES|QL 的拒绝规则。"""
import pytest

from logsearch import raw
from logsearch.common import LogError
from logsearch_fakes import AST_DIR, ast_fixture
from logsearch.clickhouse import unescape_tsv


def check(name):
    _, text = ast_fixture(name)
    lines = [unescape_tsv(line) for line in text.split('\n') if line.strip()]
    raw.check_ast(raw.parse_ast(lines))


def rejected(name):
    with pytest.raises(LogError) as error:
        check(name)
    assert error.value.code == 'raw_invalid' and error.value.status == 400
    return error.value.message


# ---------------------------------------------------------------------------
# AST 规则（样本）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('name', ['ph_simple', 'ph_self_join', 'ph_subquery_in', 'ph_scalar_cte', 'ph_union',
                                  'ph_array_join', 'ph_window', 'ph_lambda', 'ph_qualified_col'])
def test_placeholder_samples_pass(name):
    check(name)


@pytest.mark.parametrize('name, message', [
    ('ph_scope_cte', '不支持 WITH 定义的表子查询（access）'),  # 作用域 CTE：外层裸写的 access 会读默认库的表
    ('ph_table_cte', '请把 WITH x AS (子查询) 改成 FROM (子查询)'),
    ('ph_direct_table', '不能直接查表 logs.access'),
    ('ph_mixed_direct', '不能直接查表 logs.access'),
    ('ph_file_func', '不能用函数 file'),
    ('ph_getsetting', '不能用函数 getSetting'),
    ('ph_in_identifier', 'IN 后面不能直接写表名或名字'),
    ('ph_settings_sub', '不能写 SETTINGS'),
])
def test_placeholder_samples_rejected(name, message):
    assert message in rejected(name)


@pytest.mark.parametrize('name, message', [
    ('simple', '不能直接查表 logs.access'),  # 直接写绑定的表名也不行（绕过集群过滤）
    ('cte_table', '不能直接查表 logs.access'),
    ('nested_cte', '不能直接查表 logs.access'),
    ('cte_shadow', '不能直接查表'),
    ('cte_join_other', '不能直接查表'),
    ('bare_table', '不能查表 access'),
    ('join_url', '不能直接查表 logs.access'),
    ('remote', '不能用表函数 remote'),
    ('numbers', '不能用表函数 numbers'),
    ('view_fn', '不能用表函数 view'),
    ('merge_fn', '不能用表函数 merge'),
    ('cluster_fn', '不能用表函数 cluster'),
    ('input_fn', '不能用表函数 input'),
    ('file_fn', '不能用表函数 file'),
    ('executable_fn', '不能用表函数 executable'),
    ('system_table', '不能直接查表 system.users'),
    ('dictget', '不能用函数 dictGet'),
    ('joinget', '不能用函数 joinGet'),
    ('settings', '不能直接查表 logs.access'),  # SETTINGS 的规则见 ph_settings_sub
    ('format', '不能写 FORMAT、INTO OUTFILE'),
    ('outfile', '不能写 FORMAT、INTO OUTFILE'),
    ('insert', '只能写一条 SELECT 查询'),
    ('show', '只能写一条 SELECT 查询'),
    ('describe', '只能写一条 SELECT 查询'),
    ('union', '不能直接查表'),
    ('exists', '不能直接查表'),
    ('scalar_subquery', '不能直接查表'),
    ('in_table', '不能直接查表'),
    ('table_star_db', '不能直接查表'),
])
def test_real_table_samples_rejected(name, message):
    assert message in rejected(name)


def test_every_fixture_is_covered():
    names = {path.stem for path in AST_DIR.glob('*.txt')}
    covered = {'ph_simple', 'ph_self_join', 'ph_subquery_in', 'ph_scalar_cte', 'ph_union', 'ph_array_join',
               'ph_window', 'ph_lambda', 'ph_qualified_col', 'ph_scope_cte', 'ph_table_cte', 'ph_direct_table',
               'ph_mixed_direct', 'ph_file_func', 'ph_getsetting', 'ph_in_identifier', 'ph_settings_sub', 'simple',
               'cte_table', 'nested_cte', 'cte_shadow', 'cte_join_other', 'bare_table', 'join_url', 'remote', 'numbers',
               'view_fn', 'merge_fn', 'cluster_fn', 'input_fn', 'file_fn', 'executable_fn', 'system_table', 'dictget',
               'joinget', 'settings', 'format', 'outfile', 'insert', 'show', 'describe', 'union', 'exists',
               'scalar_subquery', 'in_table', 'table_star_db',
               # 下面这些都直接写了表名（被 TableIdentifier 规则拒绝），在 test_more_real_samples 里统一检查
               'array_join', 'comment_trick', 'cte_scalar', 'final_sample', 'in_bare_table', 'in_subquery',
               'in_tuple', 'lambda', 'quantiles', 'window', 'multi', 'self_join'}
    assert names == covered


@pytest.mark.parametrize('name', ['array_join', 'comment_trick', 'cte_scalar', 'final_sample', 'in_bare_table',
                                  'in_subquery', 'in_tuple', 'lambda', 'quantiles', 'window', 'self_join'])
def test_more_real_samples(name):
    assert '不能直接查表 logs.access' in rejected(name)


def test_multi_statement_sample_is_a_server_error():
    sql, text = ast_fixture('multi')
    assert text.startswith('HTTP 400') and 'Multi-statements' in text
    with pytest.raises(LogError) as error:  # 词法阶段就拒绝
        raw.prepare_sql(sql)
    assert '只能写一条语句' in error.value.message


@pytest.mark.parametrize('lines, message', [
    ([], '只能写一条 SELECT 查询'),
    (['SelectWithUnionQuery (children 1)', 'SelectWithUnionQuery (children 1)'], '只能写一条 SELECT 语句'),
    (['SelectWithUnionQuery (children 1)', ' ExpressionList (children 1)', '  SelectQuery (children 1)',
      '   ExpressionList (children 1)', '    Function DICTGETSTRING (children 1)', '     ExpressionList'],
     '不能用函数 DICTGETSTRING'),
    (['SelectWithUnionQuery (children 1)', ' ExpressionList (children 1)', '  SelectQuery (children 1)',
      '   Function globalNotNullIn (children 1)', '    ExpressionList (children 2)', '     Identifier a',
      '     Identifier t'], 'IN 后面不能直接写表名或名字'),
    (['SelectWithUnionQuery (children 1)', ' ExpressionList (children 1)', '  SelectQuery (children 1)',
      '   Function s3Cluster (children 1)'], '不能用函数 s3Cluster'),
])
def test_constructed_trees(lines, message):
    with pytest.raises(LogError) as error:
        raw.check_ast(raw.parse_ast(lines))
    assert message in error.value.message


def test_parse_node_details():
    node = raw.parse_node('      TableIdentifier __kd_src__ (alias a)')
    assert (node.kind, node.name, node.alias) == ('TableIdentifier', '__kd_src__', 'a')
    node = raw.parse_node('    Function count (alias c) (children 1)')
    assert (node.kind, node.name, node.alias) == ('Function', 'count', 'c')
    node = raw.parse_node("         Literal 'x (alias y)'")
    assert node.kind == 'Literal' and node.alias is None
    node = raw.parse_node('InsertQuery   (children 3)')
    assert node.kind == 'InsertQuery' and node.name == ''


# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------
def test_prepare_sql_finds_src_only_in_code():
    sql = ("SELECT '$src', \"$src\", `$src`, $$ $src $$, $t$ x $src $t$ /* $src /* nested */ $src */ -- $src\n"
           "# $src\n, count() FROM $src AS a JOIN $src b ON a.x = b.x;")
    clean, spans = raw.prepare_sql(sql)
    assert not clean.endswith(';') and len(spans) == 2
    assert [clean[start:end] for start, end in spans] == ['$src', '$src']
    placeholder = raw.substitute(clean, spans, raw.PLACEHOLDER)
    assert placeholder.count('__kd_src__') == 2 and "'$src'" in placeholder
    assert placeholder.endswith('FROM __kd_src__ AS a JOIN __kd_src__ b ON a.x = b.x')


@pytest.mark.parametrize('sql, message', [
    ('', 'sql 要是一条 SELECT 语句'),
    (None, 'sql 要是一条 SELECT 语句'),
    ('SELECT 1', '没有 $src'),
    ("SELECT '$src'", '没有 $src'),
    ('SELECT 1 -- $src', '没有 $src'),
    ('SELECT $srcx FROM t', '没有 $src'),
    ('SELECT count() FROM $src; SELECT 1', '只能写一条语句'),
    ('SELECT count() FROM $src;;', '只能写一条语句'),
    ('SELECT 1 FROM __kd_src__', '不能出现 __kd_src__'),
    ("SELECT '__KD_SRC__' FROM $src", '不能出现 __kd_src__'),
    ("SELECT 'abc FROM $src", '引号没有结束'),
    ('SELECT 1 FROM $src /* x', '注释 /* 没有结束'),
    ('SELECT $tag$ x FROM $src', '没有结束'),
    ('SELECT file $src', '$src 前面不能紧跟 file'),  # 替换后会变成 file((SELECT …))
    ('SELECT x FROM t WHERE id = getSetting $src', '$src 前面不能紧跟 getSetting'),
    ('SELECT `dictGet` $src', '$src 前面不能紧跟 `dictGet`'),
    ('SELECT 1 FROM $src' + ' ' * raw.MAX_SQL, 'sql 最长'),
])
def test_prepare_sql_rejects(sql, message):
    with pytest.raises(LogError) as error:
        raw.prepare_sql(sql)
    assert error.value.code == 'raw_invalid' and message in error.value.message


def test_prepare_sql_allows_from_join_comma_paren():
    for sql in ('SELECT * FROM $src', 'select * from\n  $src', 'SELECT * FROM $src a LEFT JOIN $src b USING id',
                'SELECT * FROM ($src) ', 'SELECT * FROM x, $src', 'SELECT (SELECT max(d) FROM $src) AS m',
                'SELECT 1 WHERE 1 OR $src', 'SELECT * FROM $src WHERE id IN $src', 'SELECT EXISTS $src'):
        raw.prepare_sql(sql)


# ---------------------------------------------------------------------------
# Elasticsearch DSL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('dsl, message', [
    ('x', 'dsl 要是 JSON 对象'),
    ({'query': {}, 'post_filter': {}}, '不支持 post_filter'),
    ({'aggs': {'all': {'global': {}, 'aggs': {'c': {'value_count': {'field': 'a'}}}}}}, '不能用 global'),
    ({'sort': [{'_script': {'type': 'number', 'script': 'x'}}]}, '不能用 _script'),
    ({'query': {'script_score': {'query': {'match_all': {}}}}}, '不能用 script_score'),
    ({'query': {'bool': {'filter': [{'script': {'script': 'true'}}]}}}, '不能用 script'),
    ({'aggs': {'x': {'scripted_metric': {}}}}, '不能用 scripted_metric'),
    ({'aggs': {'x': {'bucket_selector': {}}}}, '不能用 bucket_selector'),
    ({'query': {'more_like_this': {'like': [{'_index': 'other', '_id': '1'}]}}}, '不能用 more_like_this'),
    ({'query': {'percolate': {}}}, '不能用 percolate'),
    ({'query': {'geo_shape': {'x': {'indexed_shape': {'index': 'other'}}}}}, '不能用 indexed_shape'),
    ({'query': {'terms': {'user': {'index': 'other', 'id': '1', 'path': 'u'}}}}, '不能用 terms lookup'),
    # 下面几种聚合会带出集群过滤之外的数据：整个索引里没命中的取值、整个索引的背景计数
    ({'aggs': {'p': {'terms': {'field': 'path', 'min_doc_count': 0}}}}, 'terms 聚合的 min_doc_count 不能是 0'),
    ({'aggs': {'p': {'multi_terms': {'terms': [{'field': 'a'}], 'min_doc_count': '0'}}}}, 'min_doc_count 不能是 0'),
    ({'aggs': {'s': {'significant_terms': {'field': 'env'}}}}, '不能用 significant_terms'),
    ({'aggs': {'s': {'significant_text': {'field': 'message'}}}}, '不能用 significant_text'),
    ({'aggs': {'s': {'rare_terms': {'field': 'x', 'background_filter': {}}}}}, '不能用 background_filter'),
    ({'query': {'match_all': {}}, 'runtime_mappings': {}}, '不支持 runtime_mappings'),
    ({'size': -1}, 'dsl.size 要是非负整数'),
    ({'size': True}, 'dsl.size 要是非负整数'),
    ({'track_total_hits': 'all'}, 'track_total_hits 要是'),
    ({'query': []}, 'dsl.query 要是对象'),
    ({'aggs': {}, 'aggregations': {}}, 'aggs 和 aggregations 只能写一个'),
    ({'query': {'match': {'message': 'x' * 50001}}}, '序列化后最长 50000'),
])
def test_dsl_rejects(dsl, message):
    with pytest.raises(LogError) as error:
        raw.check_dsl(dsl, 100)
    assert error.value.code == 'raw_invalid' and message in error.value.message


def test_dsl_nesting_limit():
    deep = {'match_all': {}}
    for _ in range(raw.MAX_DEPTH + 2):
        deep = {'bool': {'must': [deep]}}
    with pytest.raises(LogError, match='嵌套太深'):
        raw.check_dsl({'query': deep}, 10)


def test_dsl_allows_ordinary_aggregations():
    raw.check_dsl({'aggs': {'t': {'date_histogram': {'field': '@timestamp', 'fixed_interval': '1m', 'min_doc_count': 0},
                                  'aggs': {'p': {'terms': {'field': 'path', 'min_doc_count': 2}}}}}}, 10)


def test_dsl_normalize_and_wrap():
    dsl = raw.check_dsl({'query': {'match': {'message': 'timeout'}}, 'size': 1000, 'track_total_hits': True,
                         'aggs': {'by': {'terms': {'field': 'level', 'size': 5}}}, '_source': ['message']}, 100)
    assert dsl['size'] == 100 and dsl['track_total_hits'] == 10000
    assert raw.check_dsl({}, 50)['size'] == 50 and raw.check_dsl({'track_total_hits': 99999}, 5)['track_total_hits'] \
        == 10000
    assert raw.check_dsl({'track_total_hits': False}, 5)['track_total_hits'] is False
    base = {'bool': {'filter': [{'range': {'@timestamp': {'gte': 1, 'lt': 2, 'format': 'epoch_millis'}}},
                                {'term': {'env': 'prod-a'}}], 'must_not': [{'term': {'env': 'x'}}]}}
    wrapped = raw.wrap_dsl(dsl, base)
    assert wrapped['query']['bool']['filter'] == base['bool']['filter']
    assert wrapped['query']['bool']['must_not'] == [{'term': {'env': 'x'}}]
    assert wrapped['query']['bool']['must'] == [{'match': {'message': 'timeout'}}]
    assert wrapped['aggs'] == dsl['aggs'] and wrapped['size'] == 100
    plain = raw.wrap_dsl(raw.check_dsl({'size': 0}, 5), base)
    assert plain['query']['bool']['must'] == [{'match_all': {}}]
    assert base['bool'].get('must') is None  # 不改调用方的对象


def test_trim_json_and_mask_tree():
    from logsearch.mask import Masker

    tree = {'by': {'buckets': [{'key': f'k{i}' * 50, 'doc_count': i} for i in range(200)]},
            'password': 'p', 'url': 'http://u:pw@h/x?token=abc'}
    masked = raw.mask_tree(Masker(), tree)
    assert masked['password'] == '***' and masked['url'] == 'http://u:***@h/x?token=***'
    trimmed, cut = raw.trim_json(masked, 2000)
    assert cut and 1 <= len(trimmed['by']['buckets']) < 200
    same, cut = raw.trim_json({'a': 1}, 100)
    assert same == {'a': 1} and cut is False


# ---------------------------------------------------------------------------
# ES|QL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('esql, expected', [
    ('| STATS c = COUNT(*) BY level', 'STATS c = COUNT(*) BY level'),
    ('WHERE level == "ERROR" | KEEP message | SORT @timestamp DESC',
     'WHERE level == "ERROR" | KEEP message | SORT @timestamp DESC'),
    ('| WHERE message LIKE "*from*" | EVAL x = "a|b"', 'WHERE message LIKE "*from*" | EVAL x = "a|b"'),
    ('| WHERE message == """say "join" | LOOKUP""" | LIMIT 5', 'WHERE message == """say "join" | LOOKUP""" | LIMIT 5'),
    ('| EVAL `from` = 1 | DROP `from`', 'EVAL `from` = 1 | DROP `from`'),
    ('| DISSECT message "%{a} %{b}" | MV_EXPAND a | CHANGE_POINT c ON t', None),
])
def test_esql_allows(esql, expected):
    result = raw.check_esql(esql)
    if expected is not None:
        assert result == expected


@pytest.mark.parametrize('esql, message', [
    ('', 'esql 要是 FROM 之后的管道'),
    ('| ', '空的'),
    ('|', '空的'),
    ('| FROM other', '不能用命令 FROM'),
    ('| ROW a = 1', '不能用命令 ROW'),
    ('| ENRICH policy ON x', '不能用命令 ENRICH'),
    ('| LOOKUP JOIN other ON x', '不能用命令 LOOKUP'),
    ('| FORK (WHERE a) (WHERE b)', '不能用命令 FORK'),
    ('| SHOW INFO', '不能用命令 SHOW'),
    ('| WHERE a || b', '空的管道段'),
    ('| EVAL x = 1 METADATA _index', '不能出现 METADATA'),
    ('| WHERE x IN (FROM other)', '不能出现 FROM'),
    # 三引号字符串里有单个 "：之后的 | LOOKUP JOIN 不能被当成字符串内容
    ('| WHERE message == """a"b""" | LOOKUP JOIN other ON x', '不能用命令 LOOKUP'),
    ('| EVAL a = """a"b""" | EVAL x = 1 lookup', '不能出现 LOOKUP'),
    # 带转义的普通字符串
    ('| WHERE message == "a\\"b" | LOOKUP JOIN other ON x', '不能用命令 LOOKUP'),
    ('| WHERE a == 1 // " \n| LOOKUP JOIN o ON x', '不要写注释'),
    ('| WHERE a == 1 /* /* */ " */ | LOOKUP JOIN o ON x', '不要写注释'),
    ('| WHERE message == "abc', '字符串没有结束'),
    ('| WHERE message == "ab\ncd"', '不能跨行'),
    ('| WHERE message == """abc', '三引号字符串没有结束'),
    ('| WHERE message == """a\nb"""', '三引号字符串没有结束或跨行'),
    ('| KEEP `abc', '反引号没有结束'),
    ('| WHERE x' * 3000, 'esql 最长'),
])
def test_esql_rejects(esql, message):
    with pytest.raises(LogError) as error:
        raw.check_esql(esql)
    assert error.value.code == 'raw_invalid' and message in error.value.message


def test_esql_query_text():
    assert raw.esql_query('app-logs-*,-app-logs-old', 'STATS c = COUNT(*)', 100) == \
        'FROM app-logs-*, -app-logs-old | STATS c = COUNT(*) | LIMIT 100'


def test_result_rows_cut():
    result = {'meta': [{'name': 'a'}, {'name': 'b'}], 'data': [[1, 2], [3, 4], [5, 6]]}
    assert raw.result_rows(result, 2) == (['a', 'b'], [[1, 2], [3, 4]], True)
    assert raw.result_rows(result, 5) == (['a', 'b'], [[1, 2], [3, 4], [5, 6]], False)
    assert raw.result_rows({**result, 'rows_before_limit_at_least': 10}, 5)[2] is True
