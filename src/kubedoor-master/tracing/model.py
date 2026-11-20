"""链路数据的解析和 Span 归一化（约定 §1.2、§6.1）。

认三种格式：
- Jaeger api_v3：{"result": {"resourceSpans": [...]}}（多条链路合在一个 resourceSpans 里，按 traceId 分组）
- OTLP JSON：{"resourceSpans": [...]}（粘贴的样例，不带 result 包装）
- Jaeger UI 旧格式：{"data": [{"traceID", "spans", "processes"}]}（粘贴的样例；也认单条链路对象和链路数组）

proto3 JSON 省略零值：没有的数字按 0、没有 parentSpanId 的是根 Span、status 是 {} 的是未设置；
64 位整数（*UnixNano、intValue）是字符串。Jaeger 加在 Span 上的 @jaeger@warnings 属性放进 warnings。
"""

import base64
import binascii
import json
import re

from .fields import resolve_cluster

KINDS = {0: 'unspecified', 1: 'internal', 2: 'server', 3: 'client', 4: 'producer', 5: 'consumer'}
KIND_VALUES = frozenset(KINDS.values())
STATUSES = {0: 'unset', 1: 'ok', 2: 'error'}
WARNINGS_KEY = '@jaeger@warnings'
MAX_ERROR_MESSAGE = 300
_HEX = re.compile(r'[0-9a-fA-F]{1,64}')
# Jaeger UI 旧格式里由 Jaeger 转换出来的内部标签，不当普通属性
_UI_INTERNAL_TAGS = ('span.kind', 'otel.status_code', 'otel.status_description', 'internal.span.format',
                     'otel.scope.name', 'otel.scope.version', 'otel.library.name', 'otel.library.version')


class Span:
    """归一化后的 Span；cluster 之后的几项是按配置派生的（derive）"""
    __slots__ = ('span_id', 'parent_id', 'trace_id', 'service', 'name', 'kind', 'start_ns', 'end_ns', 'status',
                 'status_message', 'attrs', 'resource', 'scope', 'events', 'links', 'warnings',
                 'cluster', 'pod', 'namespace', 'http_status', 'peer', 'error_message')

    def __init__(self, **values):
        for key in self.__slots__:
            setattr(self, key, values.get(key))

    @property
    def duration_ns(self):
        return max(0, self.end_ns - self.start_ns)

    @property
    def duration_us(self):
        return self.duration_ns // 1000

    @property
    def duration_ms(self):
        return self.duration_ns / 1e6

    @property
    def is_error(self):
        return self.status == 'error'


# ---------------------------------------------------------------------------
# 值解码
# ---------------------------------------------------------------------------
def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default


def norm_id(value, size):
    """trace / span ID → 小写十六进制（短的左边补 0）；protobuf 默认的 base64 也认"""
    if value in (None, ''):
        return ''
    text = str(value).strip()
    if _HEX.fullmatch(text):
        text = text.lower()
        return text.rjust(size, '0') if len(text) < size else text
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return text
    return raw.hex().rjust(size, '0') if raw else ''


def decode_value(value):
    """OTLP AnyValue → Python 值：intValue→int、doubleValue→float、boolValue→bool、arrayValue→list、
    kvlistValue→dict、bytesValue→"<bytes N>"；空值 → None"""
    if not isinstance(value, dict):
        return value
    if 'stringValue' in value:
        return value['stringValue']
    if 'intValue' in value:
        return to_int(value['intValue'], value['intValue'])
    if 'doubleValue' in value:
        try:
            return float(value['doubleValue'])
        except (TypeError, ValueError):
            return value['doubleValue']
    if 'boolValue' in value:
        flag = value['boolValue']
        return flag if isinstance(flag, bool) else str(flag).lower() == 'true'
    if 'arrayValue' in value:
        items = (value['arrayValue'] or {}).get('values') if isinstance(value['arrayValue'], dict) else None
        return [decode_value(item) for item in items or []]
    if 'kvlistValue' in value:
        return decode_attrs((value['kvlistValue'] or {}).get('values') if isinstance(value['kvlistValue'], dict)
                            else None)
    if 'bytesValue' in value:
        try:
            size = len(base64.b64decode(value['bytesValue'] or ''))
        except (binascii.Error, ValueError, TypeError):
            size = len(str(value['bytesValue']))
        return f'<bytes {size}>'
    return None


def decode_attrs(items):
    result = {}
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get('key'), str) and item['key']:
            result[item['key']] = decode_value(item.get('value'))
    return result


def _kind(value):
    if isinstance(value, bool):
        return 'unspecified'
    if isinstance(value, int):
        return KINDS.get(value, 'unspecified')
    text = str(value or '').strip().lower()
    if text.isdigit():
        return KINDS.get(int(text), 'unspecified')
    text = text.removeprefix('span_kind_')
    return text if text in KIND_VALUES else 'unspecified'


def _status(status):
    status = status if isinstance(status, dict) else {}
    code = status.get('code', 0)
    if isinstance(code, str):
        text = code.strip().upper()
        code = {'STATUS_CODE_UNSET': 0, 'STATUS_CODE_OK': 1, 'STATUS_CODE_ERROR': 2, 'UNSET': 0, 'OK': 1,
                'ERROR': 2}.get(text, to_int(text, 0))
    message = status.get('message')
    return STATUSES.get(code, 'unset'), message if isinstance(message, str) else ''


def _warnings(value):
    if value in (None, '', []):
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item) for item in items if item not in (None, '')]


# ---------------------------------------------------------------------------
# OTLP（Jaeger api_v3）
# ---------------------------------------------------------------------------
def _otlp_spans(resource_spans):
    for entry in resource_spans or []:
        if not isinstance(entry, dict):
            continue
        res = entry.get('resource') if isinstance(entry.get('resource'), dict) else {}
        resource = decode_attrs(res.get('attributes'))
        service = str(resource.get('service.name') or '')
        for scope_spans in entry.get('scopeSpans') or entry.get('instrumentationLibrarySpans') or []:
            if not isinstance(scope_spans, dict):
                continue
            info = scope_spans.get('scope') or scope_spans.get('instrumentationLibrary') or {}
            info = info if isinstance(info, dict) else {}
            scope = {'name': str(info.get('name') or ''), 'version': str(info.get('version') or ''),
                     'attrs': decode_attrs(info.get('attributes'))}
            for raw in scope_spans.get('spans') or []:
                if isinstance(raw, dict):
                    yield _otlp_span(raw, resource, service, scope)


def _otlp_span(raw, resource, service, scope):
    attrs = decode_attrs(raw.get('attributes'))
    warnings = _warnings(attrs.pop(WARNINGS_KEY, None))
    status, message = _status(raw.get('status'))
    start = to_int(raw.get('startTimeUnixNano'))
    end = max(to_int(raw.get('endTimeUnixNano')), start)
    events = [{'name': str(item.get('name') or ''), 'time_ns': to_int(item.get('timeUnixNano')),
               'attrs': decode_attrs(item.get('attributes'))}
              for item in raw.get('events') or [] if isinstance(item, dict)]
    links = [{'trace_id': norm_id(item.get('traceId'), 32), 'span_id': norm_id(item.get('spanId'), 16)}
             for item in raw.get('links') or [] if isinstance(item, dict)]
    return Span(span_id=norm_id(raw.get('spanId'), 16), parent_id=norm_id(raw.get('parentSpanId'), 16),
                trace_id=norm_id(raw.get('traceId'), 32), service=service, name=str(raw.get('name') or ''),
                kind=_kind(raw.get('kind', 0)), start_ns=start, end_ns=end, status=status, status_message=message,
                attrs=attrs, resource=resource, scope=scope, events=events, links=links, warnings=warnings)


# ---------------------------------------------------------------------------
# Jaeger UI 旧格式
# ---------------------------------------------------------------------------
def _tag_value(tag):
    value, kind = tag.get('value'), str(tag.get('type') or '').lower()
    if kind == 'int64':
        return to_int(value, value)
    if kind == 'float64':
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if kind == 'bool':
        return value if isinstance(value, bool) else str(value).lower() == 'true'
    if kind == 'binary':
        return f'<bytes {len(str(value or ""))}>'
    return value


def _tags(items):
    result = {}
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get('key'), str) and item['key']:
            result[item['key']] = _tag_value(item)
    return result


def _process(process):
    process = process if isinstance(process, dict) else {}
    service = str(process.get('serviceName') or '')
    resource = _tags(process.get('tags'))
    if service:
        resource.setdefault('service.name', service)
    return service, resource


def _ui_spans(trace):
    raw_processes = trace.get('processes') if isinstance(trace.get('processes'), dict) else {}
    processes = {pid: _process(proc) for pid, proc in raw_processes.items()}
    default_trace = norm_id(trace.get('traceID'), 32)
    scopes = {}
    for raw in trace.get('spans') or []:
        if not isinstance(raw, dict):
            continue
        service, resource = processes.get(raw.get('processID')) or _process(raw.get('process'))
        trace_id = norm_id(raw.get('traceID'), 32) or default_trace
        attrs = _tags(raw.get('tags'))
        kind = _kind(attrs.get('span.kind'))
        code = str(attrs.get('otel.status_code') or '').upper()
        error_tag = attrs.get('error')
        status = 'error' if code == 'ERROR' or error_tag is True or str(error_tag).lower() == 'true' else \
            'ok' if code == 'OK' else 'unset'
        message = str(attrs.get('otel.status_description') or '')
        scope_name = str(attrs.get('otel.scope.name') or attrs.get('otel.library.name') or '')
        scope_version = str(attrs.get('otel.scope.version') or attrs.get('otel.library.version') or '')
        for key in _UI_INTERNAL_TAGS:
            attrs.pop(key, None)
        if error_tag is True or str(error_tag).lower() == 'true':
            attrs.pop('error', None)
        scope = scopes.setdefault((scope_name, scope_version), {'name': scope_name, 'version': scope_version,
                                                                'attrs': {}})
        refs = [ref for ref in raw.get('references') or [] if isinstance(ref, dict) and ref.get('spanID')]
        parent_ref = next((ref for ref in refs if ref.get('refType') == 'CHILD_OF'), refs[0] if refs else None)
        links = [{'trace_id': norm_id(ref.get('traceID'), 32) or trace_id, 'span_id': norm_id(ref.get('spanID'), 16)}
                 for ref in refs if ref is not parent_ref]
        start = to_int(raw.get('startTime')) * 1000
        events = []
        for log in raw.get('logs') or []:
            if not isinstance(log, dict):
                continue
            fields = _tags(log.get('fields'))
            name = fields.pop('event', None)
            events.append({'name': str(name or 'log'), 'time_ns': to_int(log.get('timestamp')) * 1000,
                           'attrs': fields})
        yield Span(span_id=norm_id(raw.get('spanID'), 16),
                   parent_id=norm_id(parent_ref.get('spanID'), 16) if parent_ref else '',
                   trace_id=trace_id, service=service, name=str(raw.get('operationName') or ''), kind=kind,
                   start_ns=start, end_ns=start + max(0, to_int(raw.get('duration'))) * 1000, status=status,
                   status_message=message, attrs=attrs, resource=resource, scope=scope, events=events, links=links,
                   warnings=_warnings(raw.get('warnings')))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
class TraceData:
    """一条链路：trace_id + 归一化的 Span 列表"""
    __slots__ = ('trace_id', 'spans')

    def __init__(self, trace_id, spans):
        self.trace_id, self.spans = trace_id, spans


def _spans_of(doc):
    if isinstance(doc, list):
        for item in doc:
            yield from _spans_of(item)
        return
    if not isinstance(doc, dict):
        raise ValueError('不认识的链路格式：应该是 Jaeger 接口返回的 {"result": {"resourceSpans": [...]}}、'
                         'OTLP 的 {"resourceSpans": [...]}，或 Jaeger UI 导出的 {"data": [...]}')
    if isinstance(doc.get('result'), dict):
        doc = doc['result']
    if 'resourceSpans' in doc:
        yield from _otlp_spans(doc.get('resourceSpans'))
    elif isinstance(doc.get('data'), list):
        for trace in doc['data']:
            if isinstance(trace, dict):
                yield from _ui_spans(trace)
    elif isinstance(doc.get('spans'), list):
        yield from _ui_spans(doc)
    elif not doc:
        return
    else:
        raise ValueError('不认识的链路格式：应该是 Jaeger 接口返回的 {"result": {"resourceSpans": [...]}}、'
                         'OTLP 的 {"resourceSpans": [...]}，或 Jaeger UI 导出的 {"data": [...]}')


def parse_document(doc):
    """解析后的 JSON → [TraceData]（按 traceId 分组，保持出现顺序）"""
    groups = {}
    for span in _spans_of(doc):
        if not span.span_id:
            continue
        groups.setdefault(span.trace_id, []).append(span)
    return [TraceData(trace_id, spans) for trace_id, spans in groups.items()]


def loads(text):
    """JSON 文本 → Python 对象；一行一个 JSON 的流式返回（NDJSON）也认，合成列表"""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('utf-8', errors='replace')
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg != 'Extra data':
            raise
    decoder, docs, index = json.JSONDecoder(), [], 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return docs
        doc, index = decoder.raw_decode(text, index)
        docs.append(doc)


def parse_text(text):
    """JSON 文本 → [TraceData]；不是 JSON 或格式不认识抛 ValueError"""
    try:
        doc = loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f'不是合法的 JSON：{str(exc)[:100]}') from None
    return parse_document(doc)


# ---------------------------------------------------------------------------
# 按配置派生的字段
# ---------------------------------------------------------------------------
def first_value(span, names):
    """候选属性名按顺序，先 Span 属性后资源属性，取第一个有值的"""
    for name in names:
        for source in (span.attrs, span.resource):
            value = source.get(name)
            if value not in (None, '', []):
                return value
    return None


def _http_status(value):
    if isinstance(value, bool) or value is None:
        return None
    code = to_int(value, None)
    return code if code is not None and 100 <= code <= 999 else None


def exception_info(span):
    """第一个 exception 事件的 (类型, 消息)"""
    for event in span.events:
        if event['name'] == 'exception' or 'exception.type' in event['attrs'] or 'exception.message' in event['attrs']:
            attrs = event['attrs']
            return str(attrs.get('exception.type') or ''), str(attrs.get('exception.message') or '')
    return '', ''


def _peer(span):
    attrs = span.attrs
    address = attrs.get('server.address')
    if address not in (None, ''):
        port = attrs.get('server.port')
        return f'{address}:{port}' if port not in (None, '') else str(address)
    for key in ('net.peer.name', 'peer.service'):
        if attrs.get(key) not in (None, ''):
            return str(attrs[key])
    system, name = attrs.get('db.system'), attrs.get('db.name')
    if system or name:
        return '/'.join(str(item) for item in (system, name) if item)
    for key in ('rpc.service', 'messaging.destination.name'):
        if attrs.get(key) not in (None, ''):
            return str(attrs[key])
    return ''


def _error_message(span):
    if span.status_message:
        return span.status_message[:MAX_ERROR_MESSAGE]
    kind, message = exception_info(span)
    if kind or message:
        return (f'{kind}: {message}' if kind and message else kind or message)[:MAX_ERROR_MESSAGE]
    if span.is_error:
        error_type = span.attrs.get('error.type')
        if error_type not in (None, '') and str(error_type) != str(span.http_status or ''):
            return str(error_type)[:MAX_ERROR_MESSAGE]
    return ''


def derive(spans, field_cfg, cluster_key, lookup):
    """按字段映射和集群字段算派生值：cluster、pod、namespace、http_status、peer、error_message"""
    pods, namespaces, codes = field_cfg.get('pod') or [], field_cfg.get('namespace') or [], \
        field_cfg.get('status_code') or []
    resolved = {}
    for span in spans:
        span.cluster = None  # 缓存的 Span 会按不同的配置再派生一次，先清掉
        if cluster_key:
            value = span.resource.get(cluster_key)
            if value not in (None, ''):
                value = str(value)
                if value not in resolved:
                    resolved[value] = resolve_cluster(value, lookup)
                span.cluster = resolved[value]
        pod, namespace = first_value(span, pods), first_value(span, namespaces)
        span.pod = str(pod) if pod is not None else ''
        span.namespace = str(namespace) if namespace is not None else ''
        span.http_status = _http_status(first_value(span, codes))
        span.peer = _peer(span) if span.kind in ('client', 'producer') else ''
        span.error_message = _error_message(span)
    return spans


def summarize(trace):
    """完整链路 → 搜索结果用的摘要（扫描模式用）"""
    spans = trace.spans
    ids = {span.span_id for span in spans}
    roots = sorted((span for span in spans if not span.parent_id), key=lambda span: span.start_ns)
    root = roots[0] if roots else min(spans, key=lambda span: span.start_ns)
    services = {}
    for span in spans:
        item = services.setdefault(span.service, [0, 0])
        item[0] += 1
        item[1] += span.is_error
    return {
        'start_ns': min(span.start_ns for span in spans),
        'end_ns': max(span.end_ns for span in spans),
        'root_service': root.service,
        'root_operation': root.name,
        'span_count': len(spans),
        'error_span_count': sum(span.is_error for span in spans),
        'orphan_span_count': sum(1 for span in spans if span.parent_id and span.parent_id not in ids),
        'services': [{'name': name, 'span_count': count, 'error_span_count': errors}
                     for name, (count, errors) in sorted(services.items(), key=lambda item: (-item[1][0], item[0]))],
    }
