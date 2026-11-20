"""链路追踪测试共用：构造 OTLP 链路的小工具、假的 Jaeger / ClickHouse HTTP 服务（本地起 aiohttp）。"""

import contextlib
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

BASE_NS = 1_795_203_249_935_000_000  # 2026-11-21 前后
CAPS = {'search': {'withoutServiceName': True, 'sameSpanConjunction': True,
                   'filter': {'levels': ['span', 'resource', 'scope', 'event', 'link'],
                              'operators': ['and', 'or', 'not', 'eq', 'gt', 'lt', 'gte', 'lte']}}}
KINDS = {'internal': 1, 'server': 2, 'client': 3, 'producer': 4, 'consumer': 5}


def any_value(value):
    if isinstance(value, bool):
        return {'boolValue': value}
    if isinstance(value, int):
        return {'intValue': str(value)}  # proto3 JSON：64 位整数是字符串
    if isinstance(value, float):
        return {'doubleValue': value}
    if isinstance(value, list):
        return {'arrayValue': {'values': [any_value(item) for item in value]}}
    if isinstance(value, dict):
        return {'kvlistValue': {'values': [{'key': k, 'value': any_value(v)} for k, v in value.items()]}}
    return {'stringValue': value}


def attrs(mapping):
    return [{'key': key, 'value': any_value(value)} for key, value in (mapping or {}).items()]


class TraceBuilder:
    """按 Jaeger 实际返回的样子构造链路：每个 Span 一个 resourceSpans，零值省略"""

    def __init__(self, trace_id='0af7651916cd43dd8448eb211c80319c', cluster='prod', cluster_key='k8sname'):
        self.trace_id, self.cluster, self.cluster_key = trace_id, cluster, cluster_key
        self.entries = []

    def span(self, span_id, name, *, parent='', service='svc-a', kind='server', start=0.0, duration=1.0,
             error=False, status_message='', http=None, attributes=None, resource=None, events=None, pod=None,
             cluster=None):
        span_attrs = dict(attributes or {})
        if http is not None:
            span_attrs['http.response.status_code'] = http
        res = {'service.name': service, 'k8s.pod.name': pod or f'{service}-pod-1', 'k8s.namespace.name': 'ns1'}
        cluster = self.cluster if cluster is None else cluster
        if cluster:
            res[self.cluster_key] = cluster
        res.update(resource or {})
        raw = {'traceId': self.trace_id, 'spanId': span_id, 'name': name, 'kind': KINDS[kind],
               'startTimeUnixNano': str(BASE_NS + int(start * 1_000_000)),
               'endTimeUnixNano': str(BASE_NS + int((start + duration) * 1_000_000)),
               'attributes': attrs(span_attrs), 'status': {}}
        if parent:
            raw['parentSpanId'] = parent
        if error:
            raw['status'] = {'code': 2, **({'message': status_message} if status_message else {})}
        if events:
            raw['events'] = [{'timeUnixNano': str(BASE_NS), 'name': name_, 'attributes': attrs(values)}
                             for name_, values in events]
        self.entries.append({'resource': {'attributes': attrs(res)},
                             'scopeSpans': [{'scope': {'name': 'io.opentelemetry.test', 'version': '1.0'},
                                             'spans': [raw]}]})
        return self

    def doc(self):
        return {'result': {'resourceSpans': list(self.entries)}}

    def raw(self):
        return json.dumps(self.doc()).encode()


def summary(trace_id, start_s, *, spans=3, errors=0, service='svc-a', operation='GET /x'):
    item = {'traceId': trace_id, 'rootServiceName': service, 'rootOperationName': operation,
            'minStartTimeUnixNano': str(int(start_s * 1e9)), 'maxEndTimeUnixNano': str(int(start_s * 1e9) + 5_000_000),
            'spanCount': spans, 'services': [{'name': service, 'spanCount': spans}]}
    if errors:  # proto3 JSON：0 省略
        item['errorSpanCount'] = errors
        item['services'][0]['errorSpanCount'] = errors
    return item


class FakeJaeger:
    """记录收到的请求；各接口的行为由属性决定（handler 返回 None = 404）"""

    def __init__(self):
        self.requests = []
        self.services = ['svc-a', 'svc-b', 'gateway']
        self.capabilities = CAPS
        self.operations = [{'name': 'GET /x', 'spanKind': 'server'}, {'name': 'SELECT t', 'spanKind': 'client'}]
        self.summaries = lambda query: []
        self.traces = lambda query: None
        self.trace = lambda trace_id, query: None
        self.fail = {}  # path → (status, body)

    def _log(self, request):
        self.requests.append((request.path, dict(request.query)))

    def _failure(self, request):
        for prefix, (status, body) in self.fail.items():
            if request.path.startswith(prefix):
                return web.json_response(body, status=status)
        return None

    async def handle(self, request):
        self._log(request)
        failure = self._failure(request)
        if failure is not None:
            return failure
        path, query = request.path, dict(request.query)
        if path == '/api/v3/services':
            return web.json_response({'services': self.services})
        if path == '/api/v3/capabilities':
            if self.capabilities is None:
                return web.json_response({'error': {'httpCode': 404, 'message': '404 page not found'}}, status=404)
            return web.json_response(self.capabilities)
        if path == '/api/v3/operations':
            return web.json_response({'operations': self.operations})
        if path == '/api/v3/trace-summaries':
            items = self.summaries(query)
            if isinstance(items, web.StreamResponse):
                return items
            return web.json_response({'summaries': items} if items else {})
        if path == '/api/v3/traces':
            doc = self.traces(query)
            if isinstance(doc, web.StreamResponse):
                return doc
            if doc is None:
                return web.json_response({'error': {'httpCode': 404, 'message': 'trace not found'}}, status=404)
            return web.Response(body=doc if isinstance(doc, bytes) else json.dumps(doc).encode(),
                                content_type='application/json')
        if path.startswith('/api/v3/traces/'):
            doc = self.trace(path.rsplit('/', 1)[-1], query)
            if isinstance(doc, web.StreamResponse):
                return doc
            if doc is None:
                return web.json_response({'error': {'httpCode': 404, 'message': 'trace not found'}}, status=404)
            return web.Response(body=doc if isinstance(doc, bytes) else json.dumps(doc).encode(),
                                content_type='application/json')
        return web.json_response({'error': {'httpCode': 404, 'message': 'no route'}}, status=404)

    def paths(self, prefix=''):
        return [path for path, _ in self.requests if path.startswith(prefix)]


class FakeClickHouse:
    """POST / → handler(sql, query) 返回 JSONCompact 对象或 web.Response；记录 SQL、URL 参数、原始查询串、请求头"""

    def __init__(self, handler=None):
        self.requests = []
        self.handler = handler or (lambda sql, query: compact([], []))

    async def handle(self, request):
        sql = await request.text()
        entry = {'sql': sql, 'query': dict(request.query), 'raw_query': request.rel_url.raw_query_string,
                 'headers': dict(request.headers)}
        self.requests.append(entry)
        result = self.handler(sql, entry['query'])
        if isinstance(result, web.StreamResponse):
            return result
        return web.json_response(result)


def compact(names, data):
    return {'meta': [{'name': name, 'type': 'String'} for name in names], 'data': data, 'rows': len(data)}


@contextlib.asynccontextmanager
async def serve(fake, method='GET'):
    app = web.Application()
    if method == 'POST':
        app.router.add_post('/', fake.handle)
    else:
        app.router.add_route('*', '/{tail:.*}', fake.handle)
    async with TestServer(app) as server:
        yield str(server.make_url('')).rstrip('/')


def tsv_unescape(text):
    """测试用的独立实现：ClickHouse 按 TSV 转义解析 String 参数"""
    out, i = [], 0
    mapping = {'\\': '\\', 't': '\t', 'n': '\n', 'r': '\r', '0': '\0', "'": "'"}
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text):
            out.append(mapping.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def parse_array_literal(text):
    """测试用的独立实现：['a','b'] → ['a', 'b']（元素在单引号里按转义解析）"""
    assert text.startswith('[') and text.endswith(']'), text
    items, i, body = [], 1, text
    while i < len(body) - 1:
        assert body[i] == "'", body[i:]
        i += 1
        chunk = []
        while body[i] != "'":
            if body[i] == '\\':
                chunk.append(body[i:i + 2])
                i += 2
            else:
                chunk.append(body[i])
                i += 1
        items.append(tsv_unescape(''.join(chunk)))
        i += 1
        if body[i] == ',':
            i += 1
    return items
