#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验看板 SQL:PostgreSQL 语法 + 列名是否真实存在 + 整数除法陷阱。

三层检查:
  1. 语法    —— 按 postgres 方言解析,展开 Grafana 变量后再解析。
  2. 列名    —— 用 db.sql 里 k8s_resources 的真实列做 qualify,拼错的列会被揪出来。
  3. 整数除法 —— ClickHouse 的 / 永远是浮点除法,PostgreSQL 的 integer/integer 是
                整除。原看板里有若干 x/1000,搬过来如果两边都是整型会静默算出 0。

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

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASH_DIR = ROOT / 'dashboards'
DB_SQL = ROOT.parent / 'src' / 'kubedoor-master' / 'db.sql'

SUBST = [
    ('${table}', 'k8s_resources'),
    ('${env}', "env = 'prod-k8s'"),
    ('${namespace}', "namespace = 'default'"),
    ('${deployment}', "deployment = 'my-app'"),
    ('${maxday}', '2026-09-17 12:00:00+00'),
    ('$__timeFilter(date)', "date BETWEEN '2026-08-19 00:00:00+00' AND '2026-09-18 00:00:00+00'"),
]

# PostgreSQL 里做整除的类型
INT_TYPES = {'smallint', 'integer', 'int', 'int2', 'int4', 'int8', 'bigint'}


def load_schema():
    """从 db.sql 解析出 k8s_resources 的列定义"""
    sql = DB_SQL.read_text(encoding='utf-8')
    m = re.search(r'CREATE TABLE IF NOT EXISTS k8s_resources \((.*?)\n\);', sql, re.S)
    if not m:
        raise SystemExit(f"没能从 {DB_SQL} 里找到 k8s_resources 的建表语句")
    cols = {}
    for line in m.group(1).split('\n'):
        line = line.split('--')[0].strip().rstrip(',')
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            cols[parts[0]] = parts[1].upper()
    return cols


def expand(sql):
    for k, v in SUBST:
        sql = sql.replace(k, v)
    return sql


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
    cols = load_schema()
    print(f"k8s_resources 列定义({len(cols)} 列):{', '.join(f'{k}:{v}' for k, v in list(cols.items())[:6])} ...\n")
    schema = {'k8s_resources': cols}

    total = syn_fail = col_fail = div_warn = 0
    for path in sorted(DASH_DIR.glob('*.json')):
        dash = json.loads(path.read_text(encoding='utf-8'))
        items = collect(dash)
        if not items:
            print(f"{path.name}: 无 SQL,跳过")
            continue
        print(f"{path.name}: {len(items)} 条 SQL")
        for label, raw in items:
            total += 1
            sql = expand(raw)
            try:
                tree = sqlglot.parse_one(sql, dialect='postgres')
            except ParseError as e:
                syn_fail += 1
                print(f"  语法FAIL {label}: {str(e).splitlines()[0]}")
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

            bad = find_int_division(tree, cols)
            if bad:
                div_warn += len(bad)
                status.append("整除风险: " + "; ".join(bad))

            print(f"  {'OK  ' if not status else '警告'} {label}" +
                  (("\n        " + "\n        ".join(status)) if status else ""))

    print(f"\n合计 {total} 条 | 语法失败 {syn_fail} | 列名失败 {col_fail} | 整除风险 {div_warn}")
    return 1 if (syn_fail or col_fail or div_warn) else 0


if __name__ == '__main__':
    sys.exit(main())
