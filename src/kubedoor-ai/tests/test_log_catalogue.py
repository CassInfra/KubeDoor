"""历史日志：目录操作 log_*（catalogue.py）和 Skill kubedoor-logs。

七个操作都是只读查询：POST master 的 /api/ai/internal/logs/{op}?env=<集群>，body 只有声明过的参数（参数名、取值范围
同 master 的约定）。不合格的参数、没声明的参数在建动作之前就拒绝，常见误用给出改法。页面选中的命名空间 / Deployment /
Pod 不会变成日志的过滤条件：按 K8S 过滤的参数带 k8s_ 前缀，网关也不给日志操作填页面上选的命名空间。
"""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient

from fakes import FakeStore
from kubedoor_ai import permissions
from kubedoor_ai.catalogue import EXTRA_CATALOG, EXTRA_SCHEMAS, LOG_OPERATIONS, build_request, validate_arguments
from kubedoor_ai.domain import AIError, CredentialCipher, Identity, redact
from kubedoor_ai.gateway import CATALOG, Gateway, RunContext, tool_catalog
from kubedoor_ai.http import create_app
from kubedoor_ai.runtime import SKILLS, SKILLS_ROOT, static_system_prompt

ENV = "cluster-a"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
POD = "order-7d9f8b6c5-x2x9k"

# 约定的目录表：operation → master 路径的最后一段
ROUTES = {"log_sources": "sources", "log_search": "search", "log_stats": "stats", "log_patterns": "patterns",
          "log_context": "context", "log_correlate": "correlate", "log_query": "query"}
TIME = {"start", "end", "lookback"}
# search、stats、patterns 共用的过滤条件
FILTER = {"source", "keyword", "level", "k8s_namespace", "k8s_pod", "k8s_deployment", "filters"} | TIME
# 约定里每个操作的参数：(必填, 全部)
PARAMETERS = {
    "log_sources": ([], {"source"}),
    "log_search": (["source"], FILTER | {"fields", "limit", "order"}),
    "log_stats": (["source"], FILTER | {"group_by", "interval", "metrics", "top"}),
    "log_patterns": (["source"], FILTER | {"exclude", "sample", "top"}),
    "log_context": (["source", "time"], {"source", "time", "k8s_pod", "anchor_id", "filters", "before", "after"}),
    "log_correlate": (["key", "value"], {"key", "value", "around", "sources", "limit"} | TIME),
    "log_query": (["source"], {"source", "sql", "dsl", "esql", "limit"} | TIME),
}
# filters 的每种 op，正好 10 项（上限）
FILTERS = [
    {"field": "status", "op": "gte", "value": 500},
    {"field": "duration", "op": "lt", "value": 1.5},
    {"field": "upstream_cluster", "op": "in", "value": ["outbound|8080||order.ops.svc.cluster.local", 7, ""]},
    {"field": "method", "op": "not_in", "value": ["OPTIONS"]},
    {"field": "path", "op": "prefix", "value": "/api/order"},
    {"field": "user_agent", "op": "not_contains", "value": "kube-probe"},
    {"field": "message", "op": "contains", "value": "Connection reset"},
    {"field": "kubernetes.pod.name", "op": "regex", "value": "order-[a-z0-9]+-.*"},
    {"field": "@trace_id", "op": "exists"},
    {"field": "error_code", "op": "not_exists"},
]
OTHER_FILTERS = [{"field": "host", "op": "eq", "value": "api.example.com"}, {"field": "level", "op": "ne", "value": ""},
                 {"field": "bytes_sent", "op": "gt", "value": 0}, {"field": "upstream_time", "op": "lte", "value": "3"},
                 {"field": "_id", "op": "eq", "value": "AZ2x9k"}]
# 每个操作的合法调用：合起来用到全部参数，并取到边界值
CALLS = {
    "log_sources": [{}, {"source": "ingress"}],
    "log_search": [
        {"source": "ingress", "start": "2026-11-23 10:00", "end": "2026-11-23T10:30:00+08:00", "keyword": "NullPointerException",
         "level": "error", "k8s_namespace": "ops", "k8s_pod": POD, "k8s_deployment": "order", "filters": FILTERS,
         "fields": ["start_time", "path", "status", "@timestamp", "kubernetes.pod.name"], "limit": 200, "order": "asc"},
        {"source": "app-java", "lookback": "31d", "level": "WARN", "filters": OTHER_FILTERS,
         "fields": [f"field_{index}" for index in range(50)], "limit": 1, "order": "desc"},
        {"source": "app-java", "end": "2026-11-23 10:00:00"},   # 只给 end：往前查 lookback（默认 1h）
    ],
    "log_stats": [
        {"source": "ingress", "lookback": "6h", "keyword": "timeout", "level": "error", "k8s_namespace": "ops", "k8s_pod": POD,
         "k8s_deployment": "order", "filters": OTHER_FILTERS, "group_by": ["time", "status", "upstream_cluster"], "interval": "5m",
         "metrics": [{"op": "count"}, {"op": "p95", "field": "duration"}, {"op": "p95", "field": "upstream_time"},
                     {"op": "uniq", "field": "client_ip"}, {"op": "max", "field": "bytes_sent"}], "top": 200},
        {"source": "ingress", "start": "2026-11-01", "end": "2026-12-02", "group_by": [], "top": 1},   # 正好 31 天
    ],
    "log_patterns": [
        {"source": "app-java", "lookback": "1h", "keyword": "Exception", "level": "error", "k8s_namespace": "ops",
         "k8s_pod": POD, "k8s_deployment": "order", "filters": FILTERS[:3], "exclude": ["kube-probe", "x" * 200], "sample": 5000, "top": 50},
        {"source": "app-java", "start": "2026-11-23 09:00", "end": "2026-11-23 10:00", "sample": 100, "top": 1},
    ],
    "log_context": [
        {"source": "app-java", "time": "2026-11-23T15:18:49.366+08:00", "k8s_pod": POD, "anchor_id": "AZ2x9k" * 85,
         "filters": [{"field": "host_name", "op": "eq", "value": "node-1"}], "before": 100, "after": 0},
        {"source": "app-java", "time": "2026-11-23 15:18:49", "before": 0, "after": 100},
    ],
    "log_correlate": [
        {"key": "trace_id", "value": TRACE, "around": "2026-11-23 15:18", "sources": ["ingress", "gateway", "app-java"], "limit": 100},
        {"key": "request_id", "value": "6f1c2a9e-1d2b-4c3d-8e9f-0a1b2c3d4e5f", "start": "2026-11-22 00:00", "end": "2026-11-23 00:00", "limit": 1},
        {"key": "access_id", "value": "1" * 200, "lookback": "7d"},
    ],
    "log_query": [
        {"source": "ingress", "lookback": "6h", "limit": 500,
         "sql": "SELECT status, count() AS c\nFROM $src\nWHERE path LIKE '/api/%'\tGROUP BY status\r\nORDER BY c DESC;"},
        {"source": "app-logs", "start": "2026-11-23 10:00", "end": "2026-11-23 11:00",
         "dsl": {"query": {"match_phrase": {"message": "timeout"}}, "aggs": {"by_level": {"terms": {"field": "level"}}},
                 "aggregations": {}, "size": 0, "sort": [{"@timestamp": "desc"}], "_source": ["message"], "track_total_hits": True}},
        {"source": "app-logs", "esql": "| STATS c = COUNT(*) BY level | SORT c DESC", "limit": 1},
    ],
}
# 只带必填参数的调用（log_query 另要 sql / dsl / esql 之一）
MINIMAL = {"log_sources": {}, "log_search": {"source": "ingress"}, "log_stats": {"source": "ingress"},
           "log_patterns": {"source": "app-java"}, "log_context": {"source": "app-java", "time": "2026-11-23 15:18:49"},
           "log_correlate": {"key": "trace_id", "value": TRACE}, "log_query": {"source": "ingress", "sql": "SELECT count() FROM $src"}}


def context(scope=None, permission="read"):
    scope = scope or {"env": ENV, "namespace": None, "deployment": None, "pod": None}
    return RunContext({"id": "run-1", "session_id": "session-1", "scope": scope, "skill_ids": []}, Identity("reader", permission))


class Master:
    """假 master：记下请求，回成功；status 不是 200 时回约定格式的失败"""

    def __init__(self, status=200, body=None):
        self.requests, self.status = [], status
        self.body = body or {"success": True, "data": {"source": "ingress", "rows": [], "returned": 0, "truncated": False}}

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    def bodies(self):
        return [json.loads(request.content) for request in self.requests]


def gateway(master):
    return Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "internal",
                   http=httpx.AsyncClient(transport=httpx.MockTransport(master.handle)))


def calls():
    return [(operation, call) for operation, items in CALLS.items() for call in items]


# ---- 目录和参数说明 ----

def test_catalogue_entries_follow_the_contract():
    assert LOG_OPERATIONS == set(ROUTES)
    for operation, route in ROUTES.items():
        assert EXTRA_CATALOG[operation] == ("POST", f"/api/ai/internal/logs/{route}", "historical", True)
        assert CATALOG[operation] == EXTRA_CATALOG[operation]
        # 查询类：不进「权限与审批」表、不用等 master 的策略、不审批
        assert operation not in permissions.TOOL_OPS
        for call in CALLS[operation]:
            assert permissions.catalog_ops(operation, call) == ([], True)
            assert permissions.needs_policy(operation, call) is False


@pytest.mark.parametrize("operation", sorted(ROUTES))
def test_schemas_declare_exactly_the_contract_parameters(operation):
    required, hints, description = EXTRA_SCHEMAS[operation]
    assert (required, set(hints)) == PARAMETERS[operation]
    assert set().union(*CALLS[operation]) == set(hints)   # 合法调用覆盖每个参数
    # 不声明 namespace / deployment / pod：页面选中的资源不能变成日志的过滤条件
    assert not {"namespace", "deployment", "pod"} & set(hints)
    assert all(isinstance(text, str) and text for text in (description, *hints.values()))


def test_tool_descriptions_are_static_short_and_explain_the_contract():
    entries = {entry["name"]: entry for entry in tool_catalog() if entry["name"] in LOG_OPERATIONS}
    assert set(entries) == set(ROUTES)
    prompt = static_system_prompt()
    assert prompt == static_system_prompt()
    for entry in entries.values():
        assert (entry["read_only"], entry["freshness"]) == (True, "historical")
        assert json.dumps(entry, ensure_ascii=False) in prompt
        # 「权限与审批」的配置不进工具描述
        assert "批准" not in entry["description"] and "审批" not in entry["description"]
    # 每轮都进系统提示：七个工具合起来要短
    assert sum(len(json.dumps(entry, ensure_ascii=False)) for entry in entries.values()) < 4500
    sources = entries["log_sources"]["description"]
    assert "kubedoor-logs" in sources and "北京时间" in sources and "只读" in sources and "先调用" in sources
    search = entries["log_search"]["arguments"]
    assert "北京时间" in search["start"] and "31 天" in search["lookback"] and "和 start 二选一" in search["lookback"]
    for op in ("eq", "not_in", "not_contains", "prefix", "lte", "exists", "not_exists", "regex"):
        assert op in search["filters"], op
    # stats、patterns 的过滤条件同 search（只写一遍）
    for name in ("log_stats", "log_patterns"):
        assert {field: entries[name]["arguments"][field] for field in FILTER - {"source"}} == dict.fromkeys(FILTER - {"source"}, "同 log_search")
    correlate = entries["log_correlate"]
    assert "trace_get" in correlate["description"] and "24h" in correlate["arguments"]["lookback"]
    assert "二选一" in correlate["arguments"]["around"]
    query = entries["log_query"]
    assert "$src" in query["arguments"]["sql"] and "FROM 之后" in query["arguments"]["esql"] and "三选一" in query["description"]


# ---- 请求 ----

@pytest.mark.parametrize("operation,args", calls())
def test_body_is_the_declared_parameters_and_query_only_the_cluster(operation, args):
    # 本轮集群写在参数里（env / k8s）也认，但只进 query，不进 body
    source = {**copy.deepcopy(args), "env": ENV, "k8s": ENV}
    query, body = build_request(operation, source, ENV)
    assert query == {"env": ENV}
    assert body == args
    # body 是复制出来的：之后改参数不影响已经生成的请求
    for value in source.values():
        if isinstance(value, (dict, list)):
            value.clear()
    assert body == args


@pytest.mark.parametrize("operation", sorted(ROUTES))
def test_other_cluster_is_rejected(operation):
    for field in ("env", "k8s"):
        with pytest.raises(AIError) as denied:
            build_request(operation, {**MINIMAL[operation], field: "other-cluster"}, ENV)
        assert (denied.value.status, denied.value.code) == (403, "scope_violation")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(ROUTES))
async def test_gateway_posts_the_contract_request_to_master(operation):
    master = Master()
    gw = gateway(master)
    try:
        assert await gw.choose_source(context(), operation, "auto") == "kubedoor"
        args = copy.deepcopy(CALLS[operation][0])
        result = await gw.execute_action(context(), {"source": "kubedoor", "operation": operation, "arguments": args})
        request, = master.requests
        assert request.method == "POST"
        assert request.url.path == f"/api/ai/internal/logs/{ROUTES[operation]}"
        assert dict(request.url.params) == {"env": ENV}
        assert json.loads(request.content) == CALLS[operation][0]
        assert request.headers["X-User-Name"] == "reader"
        assert result["success"] is True and result["data"] == master.body["data"]
        assert (result["source"], result["freshness"]) == ("kubedoor", "historical")
    finally:
        await gw.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,message", [
    (400, "not_configured", "系统管理员还没有配置历史日志（系统管理 → 历史日志）"),
    (404, "source_not_found", "没有日志源 nginx；当前集群可用：ingress、app-java"),
    (404, "source_not_bound", "日志源 gateway 没有绑定到集群 cluster-a；当前集群可用：ingress"),
    (400, "source_disabled", "日志源 ingress 已停用"),
    (400, "field_not_found", "没有字段 stauts，相近的字段：status"),
    (403, "raw_not_allowed", "这个连接不允许原生查询"),
    (400, "raw_invalid", "只能用 $src 引用日志源"),
    (400, "query_rejected", "Code: 47. DB::Exception: Unknown expression identifier"),
    (400, "too_expensive", "超出单次查询读取行数上限：加条件或缩短时间"),
    (504, "timeout", "查询超时"),
    (502, "datasource_unavailable", "连接 ClickHouse 失败"),
    (409, "version_unsupported", "ES|QL 要 Elasticsearch 8.14 及以上"),
    (403, "permission_denied", "没有集群 cluster-a 的访问权限"),
])
async def test_master_failures_reach_the_model_with_their_code(status, code, message):
    # master 的 ext_api.fail 格式：code 在顶层，网关原样带给模型
    master = Master(status, {"success": False, "code": code, "message": message, "error": message})
    gw = gateway(master)
    try:
        result = await gw.execute_action(context(), {"source": "kubedoor", "operation": "log_search", "arguments": {"source": "ingress"}})
        assert result["success"] is False
        assert result["code"] == code
        assert result["error"]["status"] == status and result["error"]["message"] == message
    finally:
        await gw.close()


# ---- 页面选中的资源不是日志的条件；查询不等策略、不审批 ----

SELECTED = {"env": ENV, "namespace": "ops", "deployment": {"namespace": "ops", "name": "app"}, "pod": {"namespace": "ops", "name": "app-1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(ROUTES))
async def test_selected_namespace_deployment_and_pod_never_reach_log_requests(operation):
    master = Master()
    gw = gateway(master)
    args = copy.deepcopy(MINIMAL[operation])
    try:
        result = await gw.invoke(context(SELECTED), operation, args, call_id=f"call-{operation}", mcp=True)
        assert result["success"] is True
        assert master.bodies() == [MINIMAL[operation]]
        assert dict(master.requests[0].url.params) == {"env": ENV}
        action, = gw.store.actions.values()
        # 动作记录（工具卡片、审计）里也没有页面默认值；查询类直接执行，不查表
        assert action["arguments"] == MINIMAL[operation]
        assert (action["read_only"], action["ops"], action["state"]) == (True, [], "succeeded")
        assert args == MINIMAL[operation]   # 调用方的参数没被改
    finally:
        await gw.close()


@pytest.mark.asyncio
async def test_log_queries_never_wait_for_the_permission_policy(permission_policy):
    """集群只读就能查：取不到「权限与审批」配置时照样执行，也不会去取"""
    permission_policy.policy = None
    master = Master()
    gw = gateway(master)
    try:
        for operation in sorted(ROUTES):
            result = await gw.invoke(context(permission="read"), operation, copy.deepcopy(MINIMAL[operation]),
                                     call_id=f"policy-{operation}", mcp=True)
            assert result["success"] is True, operation
        assert permission_policy.calls == 0
        assert {action["state"] for action in gw.store.actions.values()} == {"succeeded"}
        assert len(master.requests) == len(ROUTES)
    finally:
        await gw.close()


@pytest.mark.asyncio
async def test_cluster_without_read_permission_is_denied_before_master():
    master = Master()
    gw = gateway(master)
    try:
        ctx = RunContext({"id": "run-1", "session_id": "s", "scope": {"env": ENV, "namespace": None, "deployment": None, "pod": None},
                          "skill_ids": []}, Identity("nobody", "none"))
        with pytest.raises(AIError) as denied:
            await gw.invoke(ctx, "log_search", {"source": "ingress"}, call_id="call-none", mcp=True)
        assert (denied.value.status, denied.value.code) == (403, "permission_denied")
        assert master.requests == [] and gw.store.actions == {}
    finally:
        await gw.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["namespace", "deployment", "pod"])
async def test_k8s_filters_need_the_prefixed_parameters(field):
    master = Master()
    gw = gateway(master)
    try:
        for operation in ("log_search", "log_stats", "log_patterns"):
            with pytest.raises(AIError) as denied:
                await gw.invoke(context(SELECTED), operation, {**MINIMAL[operation], field: "ops"}, call_id=f"call-{operation}-{field}", mcp=True)
            assert f"k8s_{field}" in str(denied.value) and denied.value.status == 400
        assert gw.store.actions == {} and master.requests == []
    finally:
        await gw.close()


# ---- 参数校验 ----

VALID = [
    ("log_search", {"source": "a"}),
    ("log_search", {"source": "a" + "b" * 31}),
    ("log_search", {"source": "app_java-2", "lookback": "744h"}),   # 31 天
    ("log_search", {"source": "x", "lookback": "2678400s"}),
    ("log_search", {"source": "x", "start": "2026-11-23T02:00:00Z"}),
    ("log_search", {"source": "x", "keyword": "x" * 500, "fields": ["@timestamp"]}),
    ("log_search", {"source": "x", "k8s_pod": "p" * 253, "level": "x" * 50}),
    ("log_search", {"source": "x", "env": ENV, "k8s": ENV}),   # 本轮集群：由 build_request / guard_scope 检查
    ("log_stats", {"source": "x", "group_by": ["time"], "interval": "1d"}),
    ("log_stats", {"source": "x", "metrics": [{"op": "p99", "field": "upstream.duration_ms"}]}),
    ("log_patterns", {"source": "x", "exclude": [f"noise {index}" for index in range(20)]}),
    ("log_query", {"source": "x", "sql": "-- 先看分布\nselect * from $src limit 10"}),
    ("log_query", {"source": "x", "esql": 'WHERE level == "ERROR" | STATS c = COUNT(*)'}),
    ("log_query", {"source": "x", "esql": "| EVAL from_ms = 1 | KEEP from_ms"}),
    ("log_query", {"source": "x", "dsl": {}}),
    ("log_correlate", {"key": "trace_id", "value": "0af7651916cd43dd8448eb211c80319c", "end": "2026-11-23 10:00"}),
    ("log_correlate", {"key": "a", "value": "x", "sources": [f"s{index}" for index in range(20)]}),
    ("log_context", {"source": "x", "time": "2026-11-23T07:18:49.366Z"}),
    ("log_search", {"source": "x", "filters": [{"field": "order_id", "op": "eq", "value": 10 ** 400}]}),   # 大整数不转 float
]


@pytest.mark.parametrize("operation,args", VALID + calls())
def test_valid_arguments_pass(operation, args):
    validate_arguments(operation, copy.deepcopy(args))


def _search(**extra):
    return {"source": "ingress", **extra}


def _filter(**item):
    return _search(filters=[item])


INVALID = [
    # 日志源
    ("log_search", {}),
    ("log_search", {"source": None}),
    ("log_search", {"source": ""}),
    ("log_search", {"source": "Ingress"}),
    ("log_search", {"source": "1ingress"}),
    ("log_search", {"source": "a" * 33}),
    ("log_search", {"source": "ingress logs"}),
    ("log_search", {"source": ["ingress"]}),
    ("log_sources", {"source": "logs.access"}),
    ("log_sources", {"source": 1}),
    # 时间：北京时间，start 和 lookback 二选一，窗口最长 31 天
    ("log_search", _search(start="yesterday")),
    ("log_search", _search(start="2026/11/23 10:00")),
    ("log_search", _search(start="10:30")),
    ("log_search", _search(start=1795203479)),
    ("log_search", _search(end="")),
    ("log_search", _search(start="2026-11-23 11:00", end="2026-11-23 10:00")),
    ("log_search", _search(start="2026-11-23 10:00", end="2026-11-23 10:00")),
    ("log_search", _search(start="2026-11-01 00:00", end="2026-12-02 00:00:01")),
    ("log_search", _search(start="2026-11-23T10:00:00+08:00", end="2026-11-23T01:59:00Z")),
    ("log_search", _search(start="2026-11-23 10:00", lookback="1h")),
    ("log_search", _search(lookback="32d")),
    ("log_search", _search(lookback="745h")),
    ("log_search", _search(lookback="1w")),
    ("log_search", _search(lookback="0m")),
    ("log_search", _search(lookback="１h")),
    ("log_search", _search(lookback=60)),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "around": "2026-11-23 10:00", "lookback": "1h"}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "around": "2026-11-23 10:00", "start": "2026-11-23 09:00"}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "around": "2026-11-23 10:00", "end": "2026-11-23 11:00"}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "around": "10:00"}),
    ("log_context", {"source": "app-java"}),
    ("log_context", {"source": "app-java", "time": None}),
    ("log_context", {"source": "app-java", "time": "15:18:49"}),
    # 文本条件：非空、不能有换行等控制字符、长度
    ("log_search", _search(keyword="")),
    ("log_search", _search(keyword="   ")),
    ("log_search", _search(keyword="a\nb")),
    ("log_search", _search(keyword="x" * 501)),
    ("log_search", _search(keyword=123)),
    ("log_search", _search(level="")),
    ("log_search", _search(level="x" * 51)),
    ("log_search", _search(level=["error"])),
    ("log_search", _search(k8s_namespace="")),
    ("log_search", _search(k8s_pod="p" * 254)),
    ("log_search", _search(k8s_deployment="a\tb")),
    ("log_context", {"source": "app-java", "time": "2026-11-23 10:00", "anchor_id": ""}),
    ("log_context", {"source": "app-java", "time": "2026-11-23 10:00", "anchor_id": "x" * 513}),
    # 关联查询
    ("log_correlate", {"value": TRACE}),
    ("log_correlate", {"key": "trace_id"}),
    ("log_correlate", {"key": "", "value": TRACE}),
    ("log_correlate", {"key": "Trace_id", "value": TRACE}),
    ("log_correlate", {"key": "trace-id", "value": TRACE}),
    ("log_correlate", {"key": "a" * 33, "value": TRACE}),
    ("log_correlate", {"key": "trace_id", "value": ""}),
    ("log_correlate", {"key": "trace_id", "value": "x" * 201}),
    ("log_correlate", {"key": "trace_id", "value": 123456}),
    ("log_correlate", {"key": "trace_id", "value": "a\nb"}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "sources": []}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "sources": "ingress"}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "sources": ["ingress", "ingress"]}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "sources": ["Ingress"]}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "sources": [f"s{index}" for index in range(21)]}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "limit": 0}),
    ("log_correlate", {"key": "trace_id", "value": TRACE, "limit": 101}),
    # filters
    ("log_search", _search(filters=[])),
    ("log_search", _search(filters="status>=500")),
    ("log_search", _search(filters=[{"field": "status", "op": "gte", "value": 500}] * 11)),
    ("log_search", _search(filters=[["status", "gte", 500]])),
    ("log_search", _filter(field="status", operator="gte", value=500)),
    ("log_search", _filter(op="eq", value=1)),
    ("log_search", _filter(field="bad field", op="eq", value=1)),
    ("log_search", _filter(field="1abc", op="eq", value=1)),
    ("log_search", _filter(field="f" * 201, op="eq", value=1)),
    ("log_search", _filter(field="status", op="=", value=500)),
    ("log_search", _filter(field="status", op="EQ", value=500)),
    ("log_search", _filter(field="status", op=["eq"], value=500)),
    ("log_search", _filter(field="status", op="exists", value=True)),
    ("log_search", _filter(field="status", op="eq")),
    ("log_search", _filter(field="status", op="in", value=500)),
    ("log_search", _filter(field="status", op="in", value=[])),
    ("log_search", _filter(field="status", op="not_in", value=list(range(101)))),
    ("log_search", _filter(field="status", op="in", value=[[500]])),
    ("log_search", _filter(field="status", op="in", value=["x" * 501])),
    ("log_search", _filter(field="status", op="eq", value=None)),
    ("log_search", _filter(field="status", op="eq", value=True)),
    ("log_search", _filter(field="status", op="eq", value={"gte": 500})),
    ("log_search", _filter(field="status", op="gt", value=float("nan"))),
    ("log_search", _filter(field="path", op="contains", value="")),
    ("log_search", _filter(field="path", op="contains", value=500)),
    ("log_search", _filter(field="path", op="prefix", value="x" * 501)),
    ("log_search", _filter(field="path", op="regex", value="x" * 201)),
    ("log_search", _filter(field="path", op="eq", value="a\nb")),
    # fields / limit / order
    ("log_search", _search(fields=[])),
    ("log_search", _search(fields="path")),
    ("log_search", _search(fields=["path", "path"])),
    ("log_search", _search(fields=[f"f{index}" for index in range(51)])),
    ("log_search", _search(fields=["bad field"])),
    ("log_search", _search(fields=[1])),
    ("log_search", _search(limit=0)),
    ("log_search", _search(limit=201)),
    ("log_search", _search(limit=True)),
    ("log_search", _search(limit="10")),
    ("log_search", _search(limit=10.0)),
    ("log_search", _search(order="DESC")),
    ("log_search", _search(order="newest")),
    # 统计
    ("log_stats", _search(group_by="time")),
    ("log_stats", _search(group_by=["time", "a", "b", "c"])),
    ("log_stats", _search(group_by=["time", "time"])),
    ("log_stats", _search(group_by=["bad field"])),
    ("log_stats", _search(group_by=[1])),
    ("log_stats", _search(interval="2m")),
    ("log_stats", _search(interval="5m")),
    ("log_stats", _search(group_by=["status"], interval="5m")),
    ("log_stats", _search(metrics=[])),
    ("log_stats", _search(metrics={"op": "count"})),
    ("log_stats", _search(metrics=["count"])),
    ("log_stats", _search(metrics=[{"op": "count", "field": "status"}])),
    ("log_stats", _search(metrics=[{"op": "p95"}])),
    ("log_stats", _search(metrics=[{"op": "median", "field": "duration"}])),
    ("log_stats", _search(metrics=[{"op": "avg", "field": "duration", "alias": "x"}])),
    ("log_stats", _search(metrics=[{"op": "avg", "field": "bad field"}])),
    ("log_stats", _search(metrics=[{"op": "count"}, {"op": "count"}])),
    ("log_stats", _search(metrics=[{"op": "avg", "field": f"f{index}"} for index in range(6)])),
    ("log_stats", _search(top=0)),
    ("log_stats", _search(top=201)),
    # 模式归类
    ("log_patterns", _search(exclude=[])),
    ("log_patterns", _search(exclude="noise")),
    ("log_patterns", _search(exclude=[""])),
    ("log_patterns", _search(exclude=["x" * 201])),
    ("log_patterns", _search(exclude=[f"n{index}" for index in range(21)])),
    ("log_patterns", _search(sample=99)),
    ("log_patterns", _search(sample=5001)),
    ("log_patterns", _search(top=51)),
    # 上下文
    ("log_context", {"source": "app-java", "time": "2026-11-23 10:00", "before": -1}),
    ("log_context", {"source": "app-java", "time": "2026-11-23 10:00", "before": 101}),
    ("log_context", {"source": "app-java", "time": "2026-11-23 10:00", "after": 101}),
    ("log_context", {"source": "app-java", "time": "2026-11-23 10:00", "filters": [{"field": "host", "op": "eq"}]}),
    # 原生查询：sql / dsl / esql 恰好一个
    ("log_query", {"source": "ingress"}),
    ("log_query", {"source": "ingress", "sql": "SELECT count() FROM $src", "dsl": {}}),
    ("log_query", {"source": "ingress", "sql": "SELECT count() FROM $src", "esql": "| LIMIT 1"}),
    ("log_query", {"source": "ingress", "sql": "SELECT count() FROM logs.ingress_access"}),
    ("log_query", {"source": "ingress", "sql": ""}),
    ("log_query", {"source": "ingress", "sql": "   "}),
    ("log_query", {"source": "ingress", "sql": "SELECT 1 FROM $src WHERE x = '" + "a" * 20000 + "'"}),
    ("log_query", {"source": "ingress", "sql": "SELECT 1 FROM $src\x00"}),
    ("log_query", {"source": "ingress", "sql": 123}),
    ("log_query", {"source": "ingress", "limit": 501, "sql": "SELECT 1 FROM $src"}),
    ("log_query", {"source": "ingress", "esql": "FROM app-logs-* | LIMIT 1"}),
    ("log_query", {"source": "ingress", "esql": "  from app-logs-* | LIMIT 1"}),
    ("log_query", {"source": "ingress", "esql": "| FROM app-logs-*"}),
    ("log_query", {"source": "ingress", "esql": "| WHERE x == \"" + "a" * 20000 + "\""}),
    ("log_query", {"source": "ingress", "dsl": "{}"}),
    ("log_query", {"source": "ingress", "dsl": []}),
    ("log_query", {"source": "ingress", "dsl": {"index": "app-logs-*", "query": {}}}),
    ("log_query", {"source": "ingress", "dsl": {"script_fields": {}}}),
    ("log_query", {"source": "ingress", "dsl": {"query": {"term": {"message": "y" * 50001}}}}),
    # 没声明的参数
    ("log_search", _search(namespace="ops")),
    ("log_search", _search(size=10)),
    ("log_sources", {"keyword": "x"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,args", INVALID)
async def test_invalid_arguments_fail_before_the_action_and_http(operation, args):
    with pytest.raises(AIError) as invalid:
        validate_arguments(operation, copy.deepcopy(args))
    assert (invalid.value.status, invalid.value.code) == (400, "invalid_request")
    master = Master()
    gw = gateway(master)
    try:
        with pytest.raises(AIError):
            await gw.invoke(context(), operation, copy.deepcopy(args), call_id="bad-call", mcp=True)
        assert gw.store.actions == {} and master.requests == []
    finally:
        await gw.close()


@pytest.mark.parametrize("operation", sorted(ROUTES))
def test_every_declared_parameter_has_a_validation_rule(operation):
    """每个声明的参数都有校验分支：换成任何类型都不对的值时报参数错误（不是「没有校验规则」的 500），而且说出是哪个参数"""
    for field in EXTRA_SCHEMAS[operation][1]:
        base = next(call for call in CALLS[operation] if field in call)
        with pytest.raises(AIError) as invalid:
            validate_arguments(operation, {**copy.deepcopy(base), field: [[]]})
        assert invalid.value.status == 400, (operation, field, str(invalid.value))
        assert field in str(invalid.value), (operation, field, str(invalid.value))


ODD = [None, True, False, 0, -1, 10 ** 400, 1.5, float("inf"), float("nan"), "", " ", "x", "\n", "a" * 60000, [], [None], [[]],
       [{}], [{"field": None, "op": None, "value": None}], [{"field": "f", "op": "in", "value": [10 ** 400, None]}], {}, {"a": 1},
       {"op": "count"}]


@pytest.mark.parametrize("operation", sorted(ROUTES))
def test_odd_values_only_ever_raise_parameter_errors(operation):
    """模型传来什么都只会得到参数错误（AIError 400），不会让网关抛出别的异常"""
    for field in EXTRA_SCHEMAS[operation][1]:
        base = next(call for call in CALLS[operation] if field in call)
        for value in ODD:
            try:
                validate_arguments(operation, {**copy.deepcopy(base), field: value})
            except AIError as exc:
                assert exc.status == 400, (operation, field, value, str(exc))


HINTS = [
    ("log_search", {"namespace": "ops"}, "按 K8S 过滤用 k8s_namespace"),
    ("log_search", {"pod": POD}, "按 K8S 过滤用 k8s_pod"),
    ("log_stats", {"deployment": "order"}, "按 K8S 过滤用 k8s_deployment"),
    ("log_context", {"pod": POD}, "按 K8S 过滤用 k8s_pod"),
    ("log_context", {"namespace": "ops"}, "log_search / log_stats / log_patterns 的 k8s_namespace"),
    ("log_search", {"query": "timeout"}, "正文关键字用 keyword"),
    ("log_search", {"q": "timeout"}, "正文关键字用 keyword"),
    ("log_patterns", {"text": "timeout"}, "正文关键字用 keyword"),
    ("log_correlate", {"text": "timeout"}, "log_search 的 keyword"),
    ("log_query", {"query": "SELECT 1"}, "原生查询用 sql / dsl / esql"),
    ("log_search", {"index": "app-logs-*"}, "用 source 指定日志源"),
    ("log_query", {"table": "logs.ingress_access"}, "用 source 指定日志源"),
    ("log_search", {"size": 10}, "条数用 limit"),
    ("log_stats", {"size": 10}, "返回多少行用 top"),
    ("log_stats", {"limit": 10}, "返回多少行用 top"),
    ("log_patterns", {"size": 10}, "取多少条归类用 sample"),
    ("log_context", {"limit": 10}, "before / after"),
    ("log_search", {"trace_id": TRACE}, "按链路 ID 查各层日志用 log_correlate"),
    ("log_correlate", {"trace_id": TRACE}, "key 填链路 ID 的关联字段"),
    ("log_search", {"start_time": "2026-11-23 10:00"}, "时间用 start / end"),
    ("log_stats", {"time": "2026-11-23 10:00"}, "时间用 start / end"),
    ("log_correlate", {"time": "2026-11-23 10:00"}, "around"),
    ("log_search", {"sort": "desc"}, "排序用 order"),
    ("log_search", {"group_by": ["time"]}, "group_by 是 log_stats 的参数"),
    ("log_search", {"exclude": ["noise"]}, "exclude 是 log_patterns 的参数"),
    ("log_stats", {"fields": ["path"]}, "fields 是 log_search 的参数"),
    ("log_search", {"before": 5}, "before 是 log_context 的参数"),
]


@pytest.mark.parametrize("operation,extra,hint", HINTS)
def test_misused_parameters_get_a_hint(operation, extra, hint):
    with pytest.raises(AIError) as invalid:
        validate_arguments(operation, {**MINIMAL[operation], **extra})
    message = str(invalid.value)
    assert invalid.value.status == 400
    assert hint in message, message
    # 也列出可用的参数，模型照着改
    assert message.startswith(f"{operation} 没有参数 {next(iter(extra))}")
    assert "可用参数：" + "、".join(EXTRA_SCHEMAS[operation][1]) in message


def test_unknown_parameters_without_a_hint_are_rejected_too():
    with pytest.raises(AIError) as invalid:
        validate_arguments("log_sources", {"verbose": True, "api_key": "x"})
    assert str(invalid.value) == "log_sources 没有参数 verbose、api_key。可用参数：source"


# ---- Skill ----

SKILL = SKILLS_ROOT / "kubedoor-logs" / "SKILL.md"
SKILL_DESCRIPTION = ("查容器标准输出之外的历史日志：按管理员登记的 ClickHouse / Elasticsearch 日志源（访问日志、网关日志、业务日志）"
                     "按时间、服务、关键字查明细和统计，归纳报错模式，按请求 ID 跨入口、网关、服务串起一次请求，结合网络架构说明定位问题。")


def test_logs_skill_is_registered_with_a_valid_frontmatter():
    entry = next(item for item in SKILLS if item["id"] == "kubedoor-logs")
    assert entry == {"id": "kubedoor-logs", "name": "历史日志查询", "description": SKILL_DESCRIPTION}
    # DeepAgents 按 frontmatter 把 Skill 列进系统提示：名称要和目录一致，要有说明，说明不能超过上限（超了会被截断）
    from deepagents.middleware.skills import MAX_SKILL_DESCRIPTION_LENGTH, _parse_skill_metadata
    text = SKILL.read_text(encoding="utf-8")
    meta = _parse_skill_metadata(text, "/skills/kubedoor-logs/SKILL.md", "kubedoor-logs")
    assert meta is not None and meta["name"] == "kubedoor-logs"
    assert meta["description"] and len(meta["description"]) <= MAX_SKILL_DESCRIPTION_LENGTH
    # 模型用 read_file 读 Skill，HTTP / MCP 也会返回它，结果都经过脱敏：不能有会被打掉的写法
    assert redact(text) == text


HEADERS = {"X-Kubedoor-Token": "internal", "X-User-Name": "reader", "X-User-Permission": "read",
           "Accept": "application/json, text/event-stream"}


def decode(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    return response.json()


def test_http_and_mcp_serve_the_logs_skill():
    fake = SimpleNamespace(bootstrap=AsyncMock(return_value={"clusters": []}))
    app = create_app(fake, token="internal")
    expected = SKILL.read_text(encoding="utf-8")
    with TestClient(app) as client:
        listed = client.get("/api/ai/skills", headers=HEADERS).json()["skills"]
        assert {"id": "kubedoor-logs", "name": "历史日志查询", "description": SKILL_DESCRIPTION} in listed
        assert client.get("/api/ai/skills/kubedoor-logs", headers=HEADERS).json() == {"id": "kubedoor-logs", "content": expected}
        init = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        headers = {**HEADERS, "Mcp-Session-Id": init.headers["mcp-session-id"]}
        resources = decode(client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}}))
        assert "skills://kubedoor-logs" in {item["uri"] for item in resources["result"]["resources"]}
        read = decode(client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                                                                  "params": {"uri": "skills://kubedoor-logs"}}))
        assert read["result"]["contents"][0]["text"] == expected
