"""历史日志测试共用：假的 ClickHouse / Elasticsearch HTTP 服务（本地起 aiohttp）、AST 样本、配置样例。

表名、索引名、集群名都是通用示例（logs.access、app-logs-*、prod-a），不含任何真实环境的信息。
"""

import json
import pathlib

from aiohttp import web

from tracing_fakes import serve  # noqa: F401  同一个本地服务启动器

AST_DIR = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'logsearch_ast'
NOW = 1_795_420_800.0  # 2026-11-23 16:00:00 +08:00
NOW_MS = int(NOW * 1000)


def ast_fixture(name):
    """AST 样本：(SQL, EXPLAIN AST 原样输出的文本)"""
    text = (AST_DIR / f'{name}.txt').read_text(encoding='utf-8')
    first, _, rest = text.partition('\n')
    return first[3:].replace('\\n', '\n'), rest


def compact(names, data, types=None):
    types = types or ['String'] * len(names)
    return {'meta': [{'name': name, 'type': type_} for name, type_ in zip(names, types)], 'data': data,
            'rows': len(data), 'statistics': {'elapsed': 0.012, 'rows_read': 777, 'bytes_read': 1000}}


def ch_error(code, message, status=500, name='UNKNOWN'):
    return web.Response(status=status, text=f'Code: {code}. DB::Exception: {message}. ({name}) (version 26.7.3.19)\n',
                        headers={'X-ClickHouse-Exception-Code': str(code)})


class FakeClickHouse:
    """POST / → routes 里第一个「SQL 包含该片段」的响应（对象 = JSONCompact，str = 原样文本，可调用 = handler(sql, query)）。
    记录 SQL、URL 参数、请求头；响应带 X-ClickHouse-Summary"""

    def __init__(self, routes=None, columns=None):
        self.requests = []
        self.routes = list(routes or [])
        self.columns = columns or {}  # 'db.table' → [(name, type)]
        self.summary = {'read_rows': '1234', 'read_bytes': '5678', 'elapsed_ns': '15000000'}

    def route(self, fragment, response):
        self.routes.insert(0, (fragment, response))

    def sqls(self, fragment=''):
        return [item['sql'] for item in self.requests if fragment in item['sql']]

    async def handle(self, request):
        sql = await request.text()
        entry = {'sql': sql, 'query': dict(request.query), 'headers': dict(request.headers)}
        self.requests.append(entry)
        result = None
        if 'FROM system.columns' in sql and 'param_kd_db' in request.query:
            key = f'{request.query["param_kd_db"]}.{request.query["param_kd_table"]}'
            result = compact(['name', 'type'], [list(item) for item in self.columns.get(key, [])])
        else:
            for fragment, response in self.routes:
                if fragment in sql:
                    result = response(sql, entry['query']) if callable(response) else response
                    break
        if result is None:
            result = compact([], [])
        if isinstance(result, web.StreamResponse):
            return result
        if isinstance(result, str):
            return web.Response(text=result)
        return web.json_response(result, headers={'X-ClickHouse-Summary': json.dumps(self.summary)})


class FakeElastic:
    """按路径分派：/、/_license、/<target>/_field_caps、/_mapping、/_search、/_count、/_query、/_resolve/index、/_cat/indices、
    /_index_template、/_component_template、/_data_stream、/_ilm/policy。记录 (方法, 路径, URL 参数, 请求体)"""

    def __init__(self):
        self.requests = []
        self.version = '9.5.2'
        self.distribution = None
        self.license = 'basic'
        self.fields = {}  # target → field_caps 的 fields
        self.runtime = {}  # target → 映射 runtime 段的字段名（dynamic: runtime 时没登记的字段）
        self.searches = []  # [(条件函数(target, body), 响应或函数)]
        self.esql = lambda body: {'columns': [{'name': 'c', 'type': 'long'}], 'values': [[1]], 'took': 3}
        self.counts = {}
        self.resolve = {'indices': [], 'aliases': [], 'data_streams': []}
        self.cat = []
        self.index_templates = {'index_templates': []}
        self.component_templates = {'component_templates': []}
        self.data_streams = {}
        self.ilm = {}
        self.fail = {}  # 路径前缀 → (状态码, 响应体)

    def on_search(self, match, response):
        self.searches.insert(0, (match, response))

    def bodies(self, suffix='/_search'):
        return [body for _, path, _, body in self.requests if path.endswith(suffix)]

    async def handle(self, request):
        path = request.path
        body = None
        if request.body_exists:
            text = await request.text()
            body = json.loads(text) if text.strip() else None
        self.requests.append((request.method, path, dict(request.query), body))
        for prefix, (status, payload) in self.fail.items():
            if path.startswith(prefix):
                return web.json_response(payload, status=status)
        if path == '/':
            version = {'number': self.version}
            if self.distribution:
                version['distribution'] = self.distribution
            return web.json_response({'name': 'node-1', 'cluster_name': 'logs-es', 'version': version})
        if path == '/_license':
            return web.json_response({'license': {'type': self.license}})
        if path == '/_query':
            return web.json_response(self.esql(body))
        if path.startswith('/_resolve/index/'):
            return web.json_response(self.resolve)
        if path == '/_cat/indices':
            return web.json_response(self.cat)
        if path == '/_index_template':
            return web.json_response(self.index_templates)
        if path == '/_component_template':
            return web.json_response(self.component_templates)
        if path.startswith('/_data_stream/'):
            name = path.rsplit('/', 1)[-1]
            return web.json_response({'data_streams': [self.data_streams.get(name, {'name': name})]})
        if path.startswith('/_ilm/policy/'):
            name = path.rsplit('/', 1)[-1]
            return web.json_response({name: self.ilm.get(name, {})})
        target, _, action = path.lstrip('/').partition('/')
        if action == '_field_caps':
            return web.json_response({'indices': [target], 'fields': self.fields.get(target, {})})
        if action == '_mapping':
            runtime = {name: {'type': 'keyword'} for name in self.runtime.get(target, ())}
            return web.json_response({f'.ds-{target}-000001': {'mappings': {'runtime': runtime}}} if runtime else {})
        if action == '_count':
            return web.json_response({'count': self.counts.get(target, 0)})
        if action == '_search':
            for match, response in self.searches:
                if match(target, body or {}):
                    result = response(target, body) if callable(response) else response
                    if isinstance(result, web.StreamResponse):
                        return result
                    return web.json_response({'took': 7, 'timed_out': False, **result})
            return web.json_response({'took': 1, 'timed_out': False,
                                      'hits': {'total': {'value': 0, 'relation': 'eq'}, 'hits': []}})
        return web.json_response({'error': {'root_cause': [{'type': 'no_route', 'reason': f'no route {path}'}]}},
                                 status=404)


def caps(**kinds):
    """field_caps 的 fields：caps(message='text', level='keyword', **{'message.keyword': 'keyword'})"""
    result = {}
    for name, kind in kinds.items():
        result[name] = {kind: {'type': kind, 'searchable': True, 'aggregatable': kind != 'text'}}
    return result


def hit(doc_id, millis, source, fields=None):
    item = {'_id': doc_id, '_source': source, 'fields': {'@timestamp': [str(millis)], **(fields or {})},
            'sort': [millis]}
    return item


def hits(items, total=None, relation='eq'):
    return {'hits': {'total': {'value': len(items) if total is None else total, 'relation': relation},
                     'hits': items}}


# ---------------------------------------------------------------------------
# 配置样例
# ---------------------------------------------------------------------------
CH_COLUMNS = [('start_time', "DateTime64(3, 'Asia/Shanghai')"), ('env', 'LowCardinality(String)'),
              ('host', 'String'), ('path', 'String'), ('method', 'LowCardinality(String)'), ('status', 'UInt16'),
              ('duration', 'Float64'), ('request_id', 'String'), ('pod', 'String'), ('namespace', 'String'),
              ('message', 'String'), ('level', 'LowCardinality(String)'), ('user_token', 'String'),
              ('upstream_cluster', 'String')]
ES_FIELDS = caps(**{'@timestamp': 'date', 'env': 'keyword', 'namespace': 'keyword', 'podname': 'keyword',
                    'appname': 'keyword', 'level': 'keyword', 'message': 'text', 'message.keyword': 'keyword',
                    'trace_id': 'keyword', 'latency': 'long', 'path': 'text', 'password': 'keyword'})


def sample_config(ch_url='http://127.0.0.1:9', es_url='http://127.0.0.1:9', **overrides):
    """一份完整的页面表单（PUT 格式）：ClickHouse 访问日志 + ES 应用日志，集群 prod-a 绑定两者"""
    cfg = {
        'enabled': True,
        'architecture': '- 入口网关（ingress）→ 应用（app）',
        'connections': [
            {'id': 'ck', 'name': '日志 ClickHouse', 'type': 'clickhouse', 'url': ch_url, 'username': 'reader',
             'password': 'ch-pass', 'timeout': 10, 'max_concurrency': 2, 'max_rows_to_read': 1000000,
             'allow_raw': True, 'tls_verify': True},
            {'id': 'es', 'name': '日志 ES', 'type': 'elasticsearch', 'url': es_url, 'auth_type': 'basic',
             'username': 'elastic', 'password': 'es-pass', 'timeout': 10, 'max_concurrency': 2, 'allow_raw': True,
             'tls_verify': True},
        ],
        'correlation_keys': [
            {'key': 'trace_id', 'label': '链路 ID', 'description': 'W3C', 'pattern': '[0-9a-f]{32}', 'trace': True},
            {'key': 'request_id', 'label': '请求 ID', 'description': '', 'pattern': '', 'trace': False},
        ],
        'sources': [
            {'id': 'ingress', 'name': '入口网关访问日志', 'enabled': True, 'connection': 'ck', 'kind': 'access',
             'description': '入口网关 Envoy 的访问日志。' * 50, 'time_field': 'start_time',
             'fields': {'message': 'message', 'level': 'level', 'namespace': 'namespace', 'pod': 'pod',
                        'status': 'status', 'duration': 'duration', 'method': 'method', 'path': 'path',
                        'host': 'host', 'service': 'upstream_cluster'},
             'duration_unit': 'ms', 'level_values': {'error': ['ERROR', 'FATAL']},
             'correlations': [{'key': 'request_id', 'field': 'request_id'}, {'key': 'trace_id', 'text': 'TID:{value}'}],
             'columns': [{'name': 'upstream_cluster', 'description': '上游服务'}],
             'default_fields': [], 'mask_fields': ['host'], 'mask_patterns': [r'card=(\d{4})'],
             'default_target': 'logs.access', 'cluster_field': 'env'},
            {'id': 'app', 'name': '应用日志', 'enabled': True, 'connection': 'es', 'kind': 'app',
             'description': 'Java 应用日志', 'time_field': '@timestamp',
             'fields': {'message': 'message', 'level': 'level', 'namespace': 'namespace', 'pod': 'podname',
                        'service': 'appname', 'duration': 'latency', 'path': 'path'},
             'correlations': [{'key': 'trace_id', 'field': 'trace_id'}],
             'default_target': 'app-logs-*', 'cluster_field': 'env'},
            {'id': 'old', 'name': '停用的日志', 'enabled': False, 'connection': 'ck', 'time_field': 'start_time'},
            {'id': 'other', 'name': '没绑定的日志', 'enabled': True, 'connection': 'ck', 'time_field': 'start_time'},
        ],
        'clusters': [
            {'env': 'prod-a', 'note': '生产 A 集群', 'bindings': [
                {'source': 'ingress', 'target': 'logs.access',
                 'conditions': [{'field': 'env', 'op': 'in', 'values': ['prod-a', 'prod-a2']}], 'note': '共享表'},
                {'source': 'app', 'target': 'app-logs-*', 'conditions': [{'field': 'env', 'op': 'eq',
                                                                        'values': ['prod-a']}]},
                {'source': 'old', 'target': 'logs.access'},
            ]},
            {'env': 'prod-b', 'bindings': [{'source': 'ingress', 'target': 'logs.access_b'}]},
        ],
    }
    cfg.update(overrides)
    return cfg
