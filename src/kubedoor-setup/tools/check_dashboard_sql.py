#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验看板 SQL:PostgreSQL 语法 + 列名是否真实存在 + 整数除法陷阱。

三层检查:
  1. 语法    —— 按 Grafana 11 的规则展开变量后,按 postgres 方言解析(见下文「变量」)。
  2. 列名    —— 用建表脚本里的真实列做 qualify(master db.sql 的 k8s_resources,
                AI schema.sql 的 kubedoor_ai_runs / kubedoor_ai_sessions),拼错的列会被揪出来。
  3. 整数除法 —— ClickHouse 的 / 永远是浮点除法,PostgreSQL 的 integer/integer 是
                整除。原看板里有若干 x/1000,搬过来如果两边都是整型会静默算出 0。

变量:Grafana 的 PostgreSQL 数据源(@grafana/sql 的 SqlDatasource.interpolateVariable)
插值时,单值变量把值里的 ' 换成 '',多选或带 All 的变量给值加上引号;选 All 时自定义的
All 值原样插入。所以变量只能放值,条件写在 SQL 里:单值写 env = '${env}',带 All 的写
namespace IN (${namespace}),All 值填列名本身(namespace IN (namespace) 恒为真)。
变量值里放 env = 'x' 这样的条件片段会被转义成 env = ''x'',整个看板报语法错误。
每条 SQL 按「各变量选一个值」展开一遍,用到带 All 的变量时再按「选 All」展开一遍。

只查这三样,不验证聚合语义。真要确认结果对不对,还是得连库跑一遍。
"""

import json
import pathlib
import re
import sys

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, OptimizeError
from sqlglot.optimizer.qualify import qualify

ROOT = pathlib.Path(__file__).resolve().parents[3]
DASH_DIR = ROOT / 'src' / 'kubedoor-setup' / 'resources' / 'dashboards'
DB_SQL = ROOT / 'src' / 'kubedoor-master' / 'db.sql'
AI_SQL = ROOT / 'src' / 'kubedoor-ai' / 'kubedoor_ai' / 'schema.sql'
# 看板查询的表:(建表脚本, 表名)
TABLES = [(DB_SQL, 'k8s_resources'), (AI_SQL, 'kubedoor_ai_runs'), (AI_SQL, 'kubedoor_ai_sessions')]

# 变量的示例值:变量查询返回的原值,插进 SQL 时再按 Grafana 的规则格式化
SAMPLES = {
    'table': 'k8s_resources',
    'env': 'prod-k8s',
    'namespace': 'default',
    'deployment': 'my-app',
    'maxday': '2026-09-17 12:00:00+00',
    'channel': 'web',
}
# 变量引用 $var、[[var:fmt]]、${var:fmt},同 Grafana 的 variableRegex
VAR_REF = re.compile(r'\$(\w+)|\[\[(\w+?)(?::(\w+))?\]\]|\$\{(\w+)(?:\.([^:^\}]+))?(?::([^\}]+))?\}')
# $__timeFilter(列) 展开成该列的时间范围
TIME_RANGE = "BETWEEN '2026-08-19 00:00:00+00' AND '2026-09-18 00:00:00+00'"
# 建表语句里的表级约束,不是列
CONSTRAINTS = {'unique', 'primary', 'constraint', 'check', 'foreign', 'exclude'}

# PostgreSQL 里做整除的类型
INT_TYPES = {'smallint', 'integer', 'int', 'int2', 'int4', 'int8', 'bigint'}


def split_top_level(body):
    """按不在括号里的逗号切开建表语句的各项(一行可以写好几列)"""
    items, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ',' and depth == 0:
            items.append(body[start:i])
            start = i + 1
    items.append(body[start:])
    return items


def table_columns(path, table):
    """建表语句 + 后来 ALTER TABLE ... ADD COLUMN IF NOT EXISTS 补的列"""
    sql = re.sub(r'--[^\n]*', '', path.read_text(encoding='utf-8'))
    m = re.search(rf'CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);', sql, re.S)
    if not m:
        raise SystemExit(f"没能从 {path} 里找到 {table} 的建表语句")
    cols = {}
    for item in split_top_level(m.group(1)):
        col = re.match(r'\s*(\w+)\s+(\w+)', item)
        if col and col.group(1).lower() not in CONSTRAINTS:
            cols[col.group(1)] = col.group(2).upper()
    for name, typ in re.findall(rf'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS (\w+) (\w+)', sql):
        cols.setdefault(name, typ.upper())
    return cols


def load_schema():
    return {table: table_columns(path, table) for path, table in TABLES}


def quote_literal(value):
    return "'" + value.replace("'", "''") + "'"


def format_value(var, value, fmt):
    """按 Grafana 11 的规则把变量值写进 SQL;value 是字符串,多个值时是列表"""
    values = value if isinstance(value, list) else [value]
    if fmt == 'raw':
        return ','.join(values)
    if fmt == 'sqlstring':
        return ','.join("'" + v.replace("'", "''").replace('"', '\\"') + "'" for v in values)
    if fmt:
        raise ValueError(f'没有模拟变量格式 {fmt}')
    # 没写格式时由数据源的 interpolateVariable 决定
    if isinstance(value, list) or var.get('multi') or var.get('includeAll'):
        return ','.join(quote_literal(v) for v in values)
    return value.replace("'", "''")


def interpolate(sql, variables, select_all):
    def replace(m):
        name, fmt = m.group(1) or m.group(2) or m.group(4), m.group(3) or m.group(6)
        if name.startswith('__'):
            return m.group(0)  # $__timeFilter 等宏由数据源在后端展开
        if name not in variables:
            raise ValueError(f'看板里没有变量 {name}')
        if name not in SAMPLES:
            raise ValueError(f'变量 {name} 没有示例值,补到 SAMPLES')
        var, value = variables[name], SAMPLES[name]
        if select_all and var.get('includeAll'):
            if var.get('allValue'):
                # 自定义的 All 值不经过格式化,原样插入(其中的变量照常展开)
                return interpolate(var['allValue'], variables, select_all)
            value = [value, value + '-2']  # 没有自定义 All 值时展开成全部选项
        elif var.get('multi'):
            value = [value]
        return format_value(var, value, fmt)

    return VAR_REF.sub(replace, sql)


def expand(sql, variables, select_all=False):
    sql = interpolate(sql, variables, select_all)
    return re.sub(r'\$__timeFilter\(([^)]+)\)', rf'\1 {TIME_RANGE}', sql)


def expansions(sql, variables):
    """(标注, 展开后的 SQL):各变量选一个值;用到带 All 的变量时再加一份选 All 的"""
    yield '', expand(sql, variables)
    names = {m.group(1) or m.group(2) or m.group(4) for m in VAR_REF.finditer(sql)}
    if any(variables.get(name, {}).get('includeAll') for name in names):
        yield ' [选 All]', expand(sql, variables, select_all=True)


def collect(dash):
    out = []

    def walk(panels):
        for p in panels:
            title = (p.get('title') or '')[:26]
            for t in p.get('targets', []) or []:
                if t.get('rawSql'):
                    out.append((f"panel[{p.get('id')}] {p.get('type')} {title} ({t.get('refId')})",
                                t['rawSql']))
            if p.get('panels'):
                walk(p['panels'])

    walk(dash.get('panels', []))
    for v in dash.get('templating', {}).get('list', []):
        q = v.get('query')
        if isinstance(q, dict):
            q = q.get('rawSql') or q.get('query')
        if isinstance(q, str) and q.strip().lower().startswith(('select', 'with')):
            out.append((f"variable[{v.get('name')}]", q))
    return out


def find_int_division(tree, cols):
    """找出两边都是整型列/整型字面量的除法"""
    hits = []

    def typ(node):
        if isinstance(node, exp.Column):
            return cols.get(node.name.lower(), '')
        if isinstance(node, exp.Literal):
            return 'INTEGER' if not node.args.get('is_string') and '.' not in node.name else 'REAL'
        if isinstance(node, (exp.Sum, exp.Max, exp.Min)):
            inner = typ(node.this)
            # sum(smallint)->bigint, sum(bigint)->numeric, max(x)->x
            return 'BIGINT' if isinstance(node, exp.Sum) and inner.lower() in INT_TYPES else inner
        if isinstance(node, exp.Paren):
            return typ(node.this)
        if isinstance(node, exp.Mul):
            lt, rt = typ(node.left), typ(node.right)
            if lt.lower() in INT_TYPES and rt.lower() in INT_TYPES:
                return 'INTEGER'
            return lt if lt.lower() not in INT_TYPES else rt
        if isinstance(node, exp.Count):
            return 'BIGINT'
        return ''

    for div in tree.find_all(exp.Div):
        lt, rt = typ(div.left), typ(div.right)
        if lt and rt and lt.lower() in INT_TYPES and rt.lower() in INT_TYPES:
            hits.append(f"{div.sql(dialect='postgres')}  ({lt} / {rt})")
    return hits


def main():
    schema = load_schema()
    for table, cols in schema.items():
        print(f"{table} 列定义({len(cols)} 列):{', '.join(f'{k}:{v}' for k, v in list(cols.items())[:6])} ...")
    print()
    # 整除检查只看列名;各表同名的列类型一致(id、created_at 之类)
    types = {}
    for cols in schema.values():
        for name, typ in cols.items():
            types.setdefault(name.lower(), typ)

    total = syn_fail = col_fail = div_warn = 0
    for path in sorted(DASH_DIR.glob('*.json')):
        dash = json.loads(path.read_text(encoding='utf-8'))
        variables = {v['name']: v for v in dash.get('templating', {}).get('list', [])}
        items = collect(dash)
        if not items:
            print(f"{path.name}: 无 SQL,跳过")
            continue
        print(f"{path.name}: {len(items)} 条 SQL")
        for label, raw in items:
            try:
                variants = list(expansions(raw, variables))
            except ValueError as e:
                total += 1
                syn_fail += 1
                print(f"  变量FAIL {label}: {e}")
                continue
            for suffix, sql in variants:
                total += 1
                try:
                    tree = sqlglot.parse_one(sql, dialect='postgres')
                except ParseError as e:
                    syn_fail += 1
                    print(f"  语法FAIL {label}{suffix}: {str(e).splitlines()[0]}")
                    continue

                status = []
                # pg_tables 是系统表,不在 schema 里,跳过列校验
                if 'pg_tables' not in sql:
                    try:
                        qualify(tree.copy(), schema=schema, dialect='postgres')
                    except (OptimizeError, Exception) as e:
                        msg = str(e).splitlines()[0]
                        if 'Unknown column' in msg or 'Column' in msg or 'cannot be resolved' in msg.lower():
                            col_fail += 1
                            status.append(f"列FAIL: {msg}")

                bad = find_int_division(tree, types)
                if bad:
                    div_warn += len(bad)
                    status.append("整除风险: " + "; ".join(bad))

                print(f"  {'OK  ' if not status else '警告'} {label}{suffix}" +
                      (("\n        " + "\n        ".join(status)) if status else ""))

    print(f"\n合计 {total} 条(含选 All 的展开) | 语法失败 {syn_fail} | 列名失败 {col_fail} | 整除风险 {div_warn}")
    return 1 if (syn_fail or col_fail or div_warn) else 0


if __name__ == '__main__':
    sys.exit(main())
