"""链路追踪：OTLP / Jaeger UI 旧格式的解析和 Span 归一化（tracing/model.py）。"""
import base64
import json

import pytest

from tracing import fields, model
from tracing_fakes import BASE_NS, TraceBuilder

TRACE = '0af7651916cd43dd8448eb211c80319c'


def test_otlp_decoding_zero_values_and_warnings():
    doc = {'result': {'resourceSpans': [{
        'resource': {'attributes': [
            {'key': 'service.name', 'value': {'stringValue': 'deploy-stock-service'}},
            {'key': 'k8sname', 'value': {'stringValue': 'cassmall-prod-penglai'}},
            {'key': 'process.pid', 'value': {'intValue': '42'}},
        ]},
        'scopeSpans': [{'scope': {'name': 'io.opentelemetry.tomcat-7.0', 'version': '2.31.1-alpha'}, 'spans': [
            {'traceId': TRACE, 'spanId': '1faf340225241fd9', 'parentSpanId': '07B61329A215071B', 'name': 'POST /purchase/x',
             'kind': 2,
             'startTimeUnixNano': '1795203249936000000', 'endTimeUnixNano': '1795203249940102735',
             'attributes': [
                 {'key': 'http.response.status_code', 'value': {'intValue': '200'}},
                 {'key': 'ratio', 'value': {'doubleValue': 0.5}},
                 {'key': 'flag', 'value': {'boolValue': True}},
                 {'key': 'tags', 'value': {'arrayValue': {'values': [{'stringValue': 'a'}, {'intValue': '2'}]}}},
                 {'key': 'kv', 'value': {'kvlistValue': {'values': [{'key': 'x', 'value': {'stringValue': 'y'}}]}}},
                 {'key': 'blob', 'value': {'bytesValue': base64.b64encode(b'12345').decode()}},
                 {'key': 'empty', 'value': {}},
                 {'key': '@jaeger@warnings', 'value': {'arrayValue': {'values': [
                     {'stringValue': 'clock skew adjustment disabled; not applying calculated delta of -129.086µs'}]}}},
             ],
             'status': {}},
            # 根 Span：没有 parentSpanId 键；kind 0；status 是字符串枚举
            {'traceId': TRACE, 'spanId': '07b61329a215071b', 'name': 'root', 'startTimeUnixNano': '1795203249935000000',
             'endTimeUnixNano': '1795203249941000000', 'status': {'code': 'STATUS_CODE_ERROR', 'message': 'boom'},
             'events': [{'timeUnixNano': '1795203249936000000', 'name': 'exception', 'attributes': [
                 {'key': 'exception.type', 'value': {'stringValue': 'java.net.SocketTimeoutException'}},
                 {'key': 'exception.message', 'value': {'stringValue': 'Read timed out'}}]}],
             'links': [{'traceId': 'ffffffffffffffffffffffffffffffff', 'spanId': 'aaaaaaaaaaaaaaaa'}]},
        ]}],
    }]}}
    [trace] = model.parse_document(doc)
    assert trace.trace_id == TRACE and len(trace.spans) == 2
    span, root = trace.spans
    assert span.service == 'deploy-stock-service' and span.kind == 'server' and span.status == 'unset'
    assert span.parent_id == '07b61329a215071b'  # 十六进制统一小写
    assert span.attrs == {'http.response.status_code': 200, 'ratio': 0.5, 'flag': True, 'tags': ['a', 2],
                          'kv': {'x': 'y'}, 'blob': '<bytes 5>', 'empty': None}
    assert span.warnings == ['clock skew adjustment disabled; not applying calculated delta of -129.086µs']
    assert span.resource['process.pid'] == 42
    assert span.scope == {'name': 'io.opentelemetry.tomcat-7.0', 'version': '2.31.1-alpha', 'attrs': {}}
    assert span.duration_us == 4102 and span.start_ns == 1795203249936000000
    assert root.parent_id == '' and root.kind == 'unspecified' and root.status == 'error'
    assert root.status_message == 'boom' and root.events[0]['attrs']['exception.type'].endswith('Exception')
    assert root.links == [{'trace_id': 'f' * 32, 'span_id': 'a' * 16}]


@pytest.mark.parametrize('kind, expected', [
    (0, 'unspecified'), (1, 'internal'), (2, 'server'), (3, 'client'), (4, 'producer'), (5, 'consumer'), (9, 'unspecified'),
    ('SPAN_KIND_CLIENT', 'client'), ('server', 'server'), ('3', 'client'), (None, 'unspecified'), (True, 'unspecified'),
])
def test_kind_values(kind, expected):
    raw = {'traceId': TRACE, 'spanId': 'a1', 'name': 'x', 'kind': kind}
    [trace] = model.parse_document({'resourceSpans': [{'scopeSpans': [{'spans': [raw]}]}]})
    assert trace.spans[0].kind == expected


@pytest.mark.parametrize('status, expected', [
    ({}, ('unset', '')), (None, ('unset', '')), ({'code': 1}, ('ok', '')), ({'code': 2, 'message': 'x'}, ('error', 'x')),
    ({'code': 'STATUS_CODE_OK'}, ('ok', '')), ({'code': 'ERROR'}, ('error', '')), ({'code': '2'}, ('error', '')),
])
def test_status_values(status, expected):
    raw = {'traceId': TRACE, 'spanId': 'a1', 'name': 'x', 'status': status}
    [trace] = model.parse_document({'resourceSpans': [{'scopeSpans': [{'spans': [raw]}]}]})
    assert (trace.spans[0].status, trace.spans[0].status_message) == expected


def test_ids_short_and_base64():
    assert model.norm_id('ABC', 16) == '0000000000000abc'
    assert model.norm_id(base64.b64encode(bytes.fromhex(TRACE)).decode(), 32) == TRACE
    assert model.norm_id('', 32) == '' and model.norm_id(None, 16) == ''
    assert model.norm_id('not-an-id!', 16) == 'not-an-id!'


def test_multiple_traces_grouped_and_ndjson():
    first = TraceBuilder('a' * 32).span('1', 'a').doc()
    second = TraceBuilder('b' * 32).span('2', 'b').span('3', 'c', parent='2').doc()
    merged = {'result': {'resourceSpans': first['result']['resourceSpans'] + second['result']['resourceSpans']}}
    traces = model.parse_document(merged)
    assert [(t.trace_id, len(t.spans)) for t in traces] == [('a' * 32, 1), ('b' * 32, 2)]
    # 一行一个 JSON（流式返回）也认；不带 result 包装的 OTLP 也认
    text = json.dumps(first) + '\n' + json.dumps(second['result'])
    assert [len(t.spans) for t in model.parse_text(text)] == [1, 2]
    assert model.parse_text('{}') == [] and model.parse_text('{"result": {}}') == []


def test_jaeger_ui_format():
    doc = {'data': [{
        'traceID': '8448eb211c80319c',
        'spans': [
            {'traceID': '8448eb211c80319c', 'spanID': 'aaa', 'operationName': 'GET /x', 'references': [],
             'startTime': 1_795_203_249_935_000, 'duration': 1500, 'processID': 'p1',
             'tags': [{'key': 'span.kind', 'type': 'string', 'value': 'server'},
                      {'key': 'http.status_code', 'type': 'int64', 'value': 500},
                      {'key': 'error', 'type': 'bool', 'value': True},
                      {'key': 'otel.scope.name', 'type': 'string', 'value': 'io.test'},
                      {'key': 'ratio', 'type': 'float64', 'value': '0.25'}],
             'logs': [{'timestamp': 1_795_203_249_936_000, 'fields': [
                 {'key': 'event', 'type': 'string', 'value': 'exception'},
                 {'key': 'exception.type', 'type': 'string', 'value': 'IOException'}]}],
             'warnings': ['clock skew']},
            {'traceID': '8448eb211c80319c', 'spanID': 'bbb', 'operationName': 'SELECT t',
             'references': [{'refType': 'FOLLOWS_FROM', 'traceID': '8448eb211c80319c', 'spanID': 'ccc'},
                            {'refType': 'CHILD_OF', 'traceID': '8448eb211c80319c', 'spanID': 'aaa'}],
             'startTime': 1_795_203_249_935_200, 'duration': 300, 'processID': 'p2',
             'tags': [{'key': 'span.kind', 'type': 'string', 'value': 'client'},
                      {'key': 'otel.status_code', 'type': 'string', 'value': 'OK'}]},
        ],
        'processes': {'p1': {'serviceName': 'svc-a', 'tags': [{'key': 'k8sname', 'type': 'string', 'value': 'prod'}]},
                      'p2': {'serviceName': 'svc-b', 'tags': []}},
    }]}
    [trace] = model.parse_document(doc)
    root, child = trace.spans
    assert trace.trace_id == '00000000000000008448eb211c80319c'
    assert (root.service, root.name, root.kind, root.status) == ('svc-a', 'GET /x', 'server', 'error')
    assert root.attrs == {'http.status_code': 500, 'ratio': 0.25}  # 内部标签（span.kind、error=true 等）去掉
    assert root.resource == {'k8sname': 'prod', 'service.name': 'svc-a'}
    assert root.scope['name'] == 'io.test' and root.warnings == ['clock skew']
    assert root.start_ns == BASE_NS and root.end_ns == BASE_NS + 1_500_000
    assert root.events == [{'name': 'exception', 'time_ns': 1_795_203_249_936_000_000,
                            'attrs': {'exception.type': 'IOException'}}]
    # CHILD_OF 优先当父 Span，其余引用当 links
    assert child.parent_id == '0000000000000aaa' and child.status == 'ok' and child.kind == 'client'
    assert child.links == [{'trace_id': '00000000000000008448eb211c80319c', 'span_id': '0000000000000ccc'}]
    # 单条链路对象、链路数组也认
    assert len(model.parse_document(doc['data'][0])[0].spans) == 2
    assert len(model.parse_document(doc['data'])[0].spans) == 2


@pytest.mark.parametrize('text, message', [
    ('not json', '不是合法的 JSON'),
    ('{"foo": 1}', '不认识的链路格式'),
    ('[1, 2]', '不认识的链路格式'),
])
def test_parse_errors(text, message):
    with pytest.raises(ValueError, match=message):
        model.parse_text(text)


def test_derive_uses_field_mapping_and_cluster():
    builder = TraceBuilder(cluster='蓬莱')
    builder.span('a1', 'POST', kind='client', http=503, attributes={
        'server.address': 'api.example.com', 'server.port': 443, 'error.type': 'java.net.ConnectException'},
        error=True)
    builder.span('a2', 'SELECT t', kind='client', parent='a1', attributes={'db.system': 'mysql', 'db.name': 'orders'})
    builder.span('a3', 'publish', kind='producer', parent='a1', attributes={'messaging.destination.name': 'topic-a'})
    builder.span('a4', 'GET /x', kind='server', parent='a1', attributes={'http.status_code': '404'},
                 resource={'k8s.pod.name': ''}, pod='', error=True,
                 events=[('exception', {'exception.type': 'NotFound', 'exception.message': 'no such order'})])
    [trace] = model.parse_document(builder.doc())
    lookup = fields.cluster_lookup({'prod': ['蓬莱']})
    model.derive(trace.spans, fields.default_fields(), 'k8sname', lookup)
    client, db, producer, server = trace.spans
    assert client.cluster == {'value': '蓬莱', 'env': 'prod'} and client.http_status == 503
    assert client.peer == 'api.example.com:443' and client.pod == 'svc-a-pod-1' and client.namespace == 'ns1'
    assert client.error_message == 'java.net.ConnectException'  # 没有状态消息和异常事件时用 error.type
    assert db.peer == 'mysql/orders' and producer.peer == 'topic-a' and db.error_message == ''
    assert server.peer == '' and server.http_status == 404  # 服务端不算 peer；旧版语义约定的状态码
    assert server.error_message == 'NotFound: no such order'
    # 字段映射改了就按新的取；没配集群字段时不派生集群
    model.derive(trace.spans, {**fields.default_fields(), 'status_code': ['nope'], 'pod': ['host.name']}, '', lookup)
    assert client.http_status is None and client.cluster is None and client.pod == ''


def test_summarize_full_trace():
    builder = TraceBuilder()
    builder.span('r', 'GET /x', duration=10).span('c', 'call', parent='r', kind='client', start=1, duration=5,
                                                   service='svc-b', error=True)
    builder.span('o', 'orphan', parent='missing', start=2, duration=1, service='svc-b')
    [trace] = model.parse_document(builder.doc())
    summary = model.summarize(trace)
    assert summary['root_service'] == 'svc-a' and summary['root_operation'] == 'GET /x'
    assert summary['span_count'] == 3 and summary['error_span_count'] == 1 and summary['orphan_span_count'] == 1
    assert summary['services'] == [{'name': 'svc-b', 'span_count': 2, 'error_span_count': 1},
                                   {'name': 'svc-a', 'span_count': 1, 'error_span_count': 0}]
    assert summary['end_ns'] - summary['start_ns'] == 10_000_000
