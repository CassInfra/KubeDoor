"""链路追踪：目录操作 trace_*（catalogue.py）和 Skill kubedoor-trace。

七个操作都是只读查询：POST master 的 /api/ai/internal/tracing/{op}?env=<集群>，body 只有声明过的参数（参数名、
取值范围同 master 的约定），不合格的参数在建动作之前就拒绝。页面选中的命名空间 / Deployment / Pod 不会变成
链路的过滤条件：按 K8S 过滤的参数带 k8s_ 前缀，网关也不给 trace 操作填页面上选的命名空间。
"""
import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient

from fakes import FakeStore
from kubedoor_ai import permissions
from kubedoor_ai.catalogue import EXTRA_CATALOG, EXTRA_SCHEMAS, TRACE_OPERATIONS, build_request, validate_arguments
from kubedoor_ai.domain import AIError, CredentialCipher, Identity, redact
from kubedoor_ai.gateway import CATALOG, Gateway, RunContext, tool_catalog
from kubedoor_ai.http import create_app
from kubedoor_ai.runtime import SKILLS, SKILLS_ROOT, static_system_prompt

ENV = "cluster-a"
TRACE_ID = "0043c22a5e5b5ca8e0c3e8885c6f3f29"
SPAN_ID = "a1b2c3d4e5f60718"

# 约定的目录表：operation → master 路径的最后一段
ROUTES = {"trace_services": "services", "trace_operations": "operations", "trace_search": "search", "trace_stats": "stats",
          "trace_get": "trace", "trace_spans": "spans", "trace_fields": "fields"}
TIME = {"start", "end", "lookback"}
# ClickHouse 加速表能表达的条件（search、stats 都有）
MATCH = {"service", "operation", "span_kind", "error", "status_codes", "status_min", "status_max", "min_duration",
         "max_duration", "method", "path", "host", "k8s_pod", "k8s_namespace", "contains"}
# 约定里每个操作的参数：(必填, 全部)
PARAMETERS = {
    "trace_services": ([], {"pattern", "limit"}),
    "trace_operations": (["service"], {"service", "span_kind", "pattern", "limit", "lookback"}),
    "trace_search": ([], MATCH | TIME | {"route", "url", "client_ip", "exception_type", "k8s_deployment", "attributes", "limit"}),
    "trace_stats": ([], MATCH | TIME | {"group_by", "interval", "top"}),
    "trace_get": (["trace_id"], {"trace_id", "around", "max_nodes", "focus_span"}),
    "trace_spans": (["trace_id"], {"trace_id", "span_ids", "errors", "around"}),
    "trace_fields": ([], {"service"}),
}
# 每个操作一组用到全部参数、取到边界值的合法调用
FULL = {
    "trace_services": {"pattern": "order", "limit": 500},
    "trace_operations": {"service": "deploy-order-service", "span_kind": "server", "pattern": "/order", "limit": 1, "lookback": "7d"},
    "trace_search": {
        "service": "deploy-gateway-api", "operation": "POST CompositeDiscoveryClient_order-service", "span_kind": "server",
        "error": True, "status_codes": [504, "5xx", "4xx", 100, 599], "status_min": 100, "status_max": 599,
        "min_duration": "9s", "max_duration": "1m30s", "method": "POST", "path": "/order-service/order/create",
        "host": "api.example.com", "k8s_pod": "deploy-gateway-api-7d9f8b6c5-x2x9k", "k8s_namespace": "ops",
        "route": "/cs/:ticketId/open", "url": "https://api.example.com/order/create?id=1", "client_ip": "10.0.0.1",
        "exception_type": "java.net.SocketTimeoutException", "k8s_deployment": "deploy-gateway-api",
        "attributes": {"biz.order_no": "SO202611210001", "http.response.status_code": "504"},
        "contains": {"query": "SO2026", "path": "/order", "biz.note": "超时"},
        "start": "2026-11-21 10:00", "end": "2026-11-21 11:00:30", "lookback": "1h", "limit": 50,
    },
    "trace_stats": {
        "service": "deploy-gateway-api", "operation": "GET /order/{id}", "span_kind": "server", "error": False,
        "status_codes": [502, 503, 504], "status_min": 400, "status_max": 499, "min_duration": "500ms", "max_duration": "30s",
        "method": "GET", "path": "/order/list", "host": "api.example.com", "k8s_pod": "deploy-gateway-api-7d9f8b6c5-x2x9k",
        "k8s_namespace": "ops", "contains": {"query": "SO2026", "operation": "order", "k8s_pod": "gateway"},
        # 10:00 北京时间到 02:30 UTC（10:30 北京时间）
        "start": "2026-11-21T10:00:00+08:00", "end": "2026-11-21T02:30:00Z", "lookback": "2d",
        "group_by": ["time", "status_code"], "interval": "5m", "top": 100,
    },
    "trace_get": {"trace_id": TRACE_ID, "around": "2026-11-21 10:30", "max_nodes": 400, "focus_span": SPAN_ID},
    "trace_spans": {"trace_id": TRACE_ID.upper(), "span_ids": [SPAN_ID, "01"], "errors": False, "around": "2026-11-21T10:30:00+08:00"},
    "trace_fields": {"service": "deploy-order-service"},
}
# 只带必填参数的调用
MINIMAL = {"trace_services": {}, "trace_operations": {"service": "deploy-order-service"}, "trace_search": {},
           "trace_stats": {}, "trace_get": {"trace_id": TRACE_ID}, "trace_spans": {"trace_id": TRACE_ID, "errors": True},
           "trace_fields": {}}


def context(scope=None, permission="read"):
    scope = scope or {"env": ENV, "namespace": None, "deployment": None, "pod": None}
    return RunContext({"id": "run-1", "session_id": "session-1", "scope": scope, "skill_ids": []}, Identity("reader", permission))


class Master:
    """假 master：记下请求，按 ROUTES 回成功；status 不是 200 时回约定格式的失败"""

    def __init__(self, status=200, body=None):
        self.requests, self.status = [], status
        self.body = body or {"success": True, "data": {"source": "clickhouse", "traces": [], "count": 0}}

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    def bodies(self):
        return [json.loads(request.content) for request in self.requests]


def gateway(master):
    return Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "internal",
                   http=httpx.AsyncClient(transport=httpx.MockTransport(master.handle)))


# ---- 目录和参数 ----

def test_catalogue_entries_follow_the_contract():
    assert TRACE_OPERATIONS == set(ROUTES)
    for operation, route in ROUTES.items():
        assert EXTRA_CATALOG[operation] == ("POST", f"/api/ai/internal/tracing/{route}", "historical", True)
        assert CATALOG[operation] == EXTRA_CATALOG[operation]
        # 查询类：不进「权限与审批」表、不用等 master 的策略
        assert operation not in permissions.TOOL_OPS
        assert permissions.catalog_ops(operation, FULL[operation]) == ([], True)
        assert permissions.needs_policy(operation, FULL[operation]) is False


@pytest.mark.parametrize("operation", sorted(ROUTES))
def test_schemas_declare_exactly_the_contract_parameters(operation):
    required, hints, description = EXTRA_SCHEMAS[operation]
    assert (required, set(hints)) == PARAMETERS[operation]
    assert set(FULL[operation]) == set(hints)   # 下面的用例覆盖每个参数
    # 不声明 namespace / deployment / pod：页面选中的资源不能变成链路的过滤条件
    assert not {"namespace", "deployment", "pod"} & set(hints)
    assert all(isinstance(text, str) and text for text in (description, *hints.values()))


def test_tool_descriptions_are_static_and_explain_the_contract():
    entries = {entry["name"]: entry for entry in tool_catalog() if entry["name"] in TRACE_OPERATIONS}
    assert entries == {entry["name"]: entry for entry in tool_catalog() if entry["name"] in TRACE_OPERATIONS}
    prompt = static_system_prompt()
    assert prompt == static_system_prompt()
    for name, entry in entries.items():
        assert (entry["read_only"], entry["freshness"]) == (True, "historical")
        assert json.dumps(entry, ensure_ascii=False) in prompt
    search, stats, get = (entries[name]["description"] for name in ("trace_search", "trace_stats", "trace_get"))
    # 工具说明只写最容易用错的约束（每轮都进系统提示），来源、失败码等细节在 Skill kubedoor-trace 里
    for phrase in ("同一个 Span", "status_codes", "status_min/status_max", "error=true", "4xx 一般不算 error",
                   "contains", "coverage / scan", "没查到不等于没发生"):
        assert phrase in search, phrase
    for phrase in ("ClickHouse 加速", "not_supported", "span_kind=server", "metrics"):
        assert phrase in stats, phrase
    assert "around" in get and "其他集群" in get
    assert "北京时间" in entries["trace_services"]["description"]
    assert "kubedoor-trace" in entries["trace_services"]["description"]
    # 状态码的写法（"4xx"、"5xx"）和耗时参数在参数说明里
    assert "\"4xx\"" in entries["trace_search"]["arguments"]["status_codes"]
    assert "Go 时长" in entries["trace_search"]["arguments"]["min_duration"]
    for name, field in (("trace_search", "start"), ("trace_get", "around"), ("trace_spans", "around")):
        assert "北京时间" in entries[name]["arguments"][field]
    # 「权限与审批」的配置不进工具描述
    assert all("批准" not in entry["description"] and "审批" not in entry["description"] for entry in entries.values())


# ---- 请求 ----

@pytest.mark.parametrize("operation", sorted(ROUTES))
def test_body_is_the_declared_parameters_and_query_only_the_cluster(operation):
    args = {**copy.deepcopy(FULL[operation]), "env": ENV, "k8s": ENV, "api_key": "must-not-forward",
            "headers": {"X-Secret": "1"}, "source": "direct"}
    query, body = build_request(operation, args, ENV)
    assert query == {"env": ENV}
    assert body == FULL[operation]
    # body 是复制出来的：之后改参数不影响已经生成的请求
    for value in args.values():
        if isinstance(value, (dict, list)):
            value.clear()
    assert body == FULL[operation]
    assert build_request(operation, copy.deepcopy(MINIMAL[operation]), ENV) == ({"env": ENV}, MINIMAL[operation])


@pytest.mark.parametrize("operation", sorted(ROUTES))
def test_other_cluster_is_rejected(operation):
    for field in ("env", "k8s"):
        with pytest.raises(AIError) as denied:
            build_request(operation, {**FULL[operation], field: "other-cluster"}, ENV)
        assert denied.value.code == "scope_violation"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(ROUTES))
async def test_gateway_posts_the_contract_request_to_master(operation):
    master = Master()
    gw = gateway(master)
    try:
        assert await gw.choose_source(context(), operation, "auto") == "kubedoor"
        result = await gw.execute_action(context(), {"source": "kubedoor", "operation": operation, "arguments": copy.deepcopy(FULL[operation])})
        request, = master.requests
        assert request.method == "POST"
        assert request.url.path == f"/api/ai/internal/tracing/{ROUTES[operation]}"
        assert dict(request.url.params) == {"env": ENV}
        assert json.loads(request.content) == FULL[operation]
        assert request.headers["X-User-Name"] == "reader"
        assert result["success"] is True and result["data"] == master.body["data"]
        assert (result["source"], result["freshness"]) == ("kubedoor", "historical")
    finally:
        await gw.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,message", [
    (400, "not_configured", "系统管理员还没有配置链路追踪（系统管理 → 链路追踪）"),
    (409, "version_unsupported", "搜索需要 Jaeger 2.23 及以上"),
    (409, "not_supported", "没有配置 ClickHouse 加速，不能统计；可以用 metrics 查 HTTP 指标"),
    (502, "jaeger_unavailable", "连接 Jaeger 超时"),
    (502, "clickhouse_unavailable", "Connection refused"),
    (404, "not_found", "没有找到这条链路"),
])
async def test_master_failures_reach_the_model_with_their_code(status, code, message):
    # master 的 ext_api.fail 格式：code 在顶层，网关原样带给模型
    master = Master(status, {"success": False, "code": code, "message": message, "error": message})
    gw = gateway(master)
    try:
        result = await gw.execute_action(context(), {"source": "kubedoor", "operation": "trace_search", "arguments": {"lookback": "15m"}})
        assert result["success"] is False
        assert result["code"] == code
        assert result["error"]["status"] == status and result["error"]["message"] == message
    finally:
        await gw.close()


# ---- 页面选中的资源不是链路的条件 ----

SELECTED = {"env": ENV, "namespace": "ops", "deployment": {"namespace": "ops", "name": "app"}, "pod": {"namespace": "ops", "name": "app-1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(ROUTES))
async def test_selected_namespace_deployment_and_pod_never_reach_trace_requests(operation):
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
async def test_other_catalogue_operations_still_default_to_the_selected_namespace():
    master = Master(body={"success": True, "data": [["app"]], "meta": [{"name": "deployment"}]})
    gw = gateway(master)
    try:
        await gw.invoke(context(SELECTED), "stored_deployments", {}, call_id="call-stored", mcp=True)
        assert dict(master.requests[0].url.params) == {"env": ENV, "namespace": "ops"}
    finally:
        await gw.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["namespace", "deployment", "pod"])
async def test_k8s_filters_need_the_prefixed_parameters(field):
    master = Master()
    gw = gateway(master)
    try:
        for operation in sorted(ROUTES):
            with pytest.raises(AIError) as denied:
                await gw.invoke(context(SELECTED), operation, {**MINIMAL[operation], field: "ops"}, call_id=f"call-{operation}-{field}", mcp=True)
            assert "k8s_namespace" in str(denied.value) and denied.value.status == 400
        assert gw.store.actions == {} and master.requests == []
    finally:
        await gw.close()


# ---- 参数校验 ----

VALID = [
    # 约定里的例子和常见写法
    ("trace_search", {"service": "deploy-gateway-api", "status_codes": [504], "lookback": "15m"}),
    ("trace_search", {"service": "deploy-gateway-api", "contains": {"path": "/order/create"}}),
    ("trace_search", {"span_kind": "server", "min_duration": "1.5s"}),
    ("trace_search", {"min_duration": "250ms", "max_duration": "1h"}),
    ("trace_search", {"min_duration": "100us", "max_duration": "100µs"}),
    ("trace_search", {"end": "2026-11-21 10:00:00"}),
    ("trace_search", {"start": "2026-11-21", "end": "2026-11-28"}),   # 只有日期按 0 点；正好 7 天
    ("trace_search", {"status_min": 500}),
    ("trace_stats", {"group_by": ["time"], "span_kind": "server", "status_codes": ["5xx"]}),
    ("trace_stats", {"group_by": []}),
    ("trace_stats", {"group_by": ["k8s_pod", "status"], "lookback": "604800s"}),
    ("trace_get", {"trace_id": "0043C22A5E5B5CA8E0C3E8885C6F3F29", "around": "2026-11-21T10:30:00Z"}),
    ("trace_get", {"trace_id": "a"}),
    ("trace_spans", {"trace_id": TRACE_ID, "span_ids": ["0" * 16] * 20}),
    ("trace_operations", {"service": "deploy-order-service", "lookback": "24h"}),
]


@pytest.mark.parametrize("operation,args", VALID + [(operation, args) for operation, args in FULL.items()])
def test_valid_arguments_pass(operation, args):
    validate_arguments(operation, copy.deepcopy(args))


INVALID = [
    ("trace_services", {"pattern": ""}),
    ("trace_services", {"pattern": "x" * 101}),
    ("trace_services", {"limit": 0}),
    ("trace_services", {"limit": 501}),
    ("trace_services", {"limit": True}),
    ("trace_services", {"limit": "10"}),
    ("trace_operations", {}),
    ("trace_operations", {"service": None}),
    ("trace_operations", {"service": ""}),
    ("trace_operations", {"service": "a", "span_kind": "SERVER"}),
    ("trace_operations", {"service": "a", "lookback": "8d"}),
    ("trace_operations", {"service": "a", "lookback": "24"}),
    ("trace_operations", {"service": "a", "lookback": "0h"}),
    ("trace_operations", {"service": "a", "lookback": "１h"}),
    ("trace_operations", {"service": "a", "limit": 501}),
    # 状态码：精确值、"4xx" / "5xx"，或者范围
    ("trace_search", {"status_codes": []}),
    ("trace_search", {"status_codes": [504] * 21}),
    ("trace_search", {"status_codes": ["504"]}),
    ("trace_search", {"status_codes": ["5XX"]}),
    ("trace_search", {"status_codes": [99]}),
    ("trace_search", {"status_codes": [600]}),
    ("trace_search", {"status_codes": [True]}),
    ("trace_search", {"status_codes": [504.0]}),
    ("trace_search", {"status_codes": 504}),
    ("trace_search", {"status_min": 99}),
    ("trace_search", {"status_max": 600}),
    ("trace_search", {"status_min": 500, "status_max": 400}),
    # 耗时：Go 时长
    ("trace_search", {"min_duration": "500"}),
    ("trace_search", {"min_duration": "-1s"}),
    ("trace_search", {"min_duration": "0s"}),
    ("trace_search", {"min_duration": "3 s"}),
    ("trace_search", {"min_duration": "3sec"}),
    ("trace_search", {"min_duration": 3}),
    ("trace_search", {"min_duration": "3s", "max_duration": "1s"}),
    ("trace_search", {"max_duration": "25h"}),
    ("trace_search", {"min_duration": "24h1s"}),
    # 时间：北京时间，窗口最长 7 天
    ("trace_search", {"start": "yesterday"}),
    ("trace_search", {"start": "2026/11/21 10:00"}),
    ("trace_search", {"start": "10:30"}),
    ("trace_search", {"start": 1795203479}),
    ("trace_search", {"start": "2026-11-21 11:00", "end": "2026-11-21 10:00"}),
    ("trace_search", {"start": "2026-11-21 10:00", "end": "2026-11-21 10:00"}),
    ("trace_search", {"start": "2026-11-01 00:00", "end": "2026-11-08 00:00:01"}),
    ("trace_search", {"start": "2026-11-21T10:00:00+08:00", "end": "2026-11-21T01:59:00Z"}),
    ("trace_search", {"lookback": "1w"}),
    ("trace_search", {"lookback": "8d"}),
    ("trace_search", {"lookback": "169h"}),
    ("trace_search", {"lookback": 60}),
    # 文本条件
    ("trace_search", {"service": ""}),
    ("trace_search", {"service": "   "}),
    ("trace_search", {"service": ["a", "b"]}),
    ("trace_search", {"path": "/a\n/b"}),
    ("trace_search", {"url": "x" * 2049}),
    ("trace_search", {"error": "true"}),
    ("trace_search", {"span_kind": "rpc"}),
    ("trace_search", {"limit": 51}),
    ("trace_search", {"limit": 0}),
    # attributes / contains
    ("trace_search", {"attributes": {}}),
    ("trace_search", {"attributes": {f"k{index}": "v" for index in range(11)}}),
    ("trace_search", {"attributes": {"biz.order_no": 123}}),
    ("trace_search", {"attributes": {"bad key": "v"}}),
    ("trace_search", {"attributes": {"k": ""}}),
    ("trace_search", {"attributes": [["k", "v"]]}),
    ("trace_search", {"contains": {"a": "1", "b": "2", "c": "3", "d": "4"}}),
    ("trace_search", {"contains": {"query": "x" * 1025}}),
    ("trace_search", {"contains": {"service": "gateway"}}),
    ("trace_search", {"contains": {"namespace": "ops"}}),
    ("trace_search", {"contains": {"pod": "app"}}),
    # 统计的参数用在搜索上
    ("trace_search", {"group_by": ["time"]}),
    ("trace_search", {"interval": "1m"}),
    ("trace_search", {"top": 10}),
    # 加速表没有的条件不能统计（静默丢掉会让统计口径变了）
    ("trace_stats", {"route": "/a"}),
    ("trace_stats", {"url": "https://api.example.com/a"}),
    ("trace_stats", {"client_ip": "10.0.0.1"}),
    ("trace_stats", {"exception_type": "java.io.IOException"}),
    ("trace_stats", {"k8s_deployment": "app"}),
    ("trace_stats", {"attributes": {"biz.order_no": "1"}}),
    ("trace_stats", {"contains": {"url": "x"}}),
    ("trace_stats", {"contains": {"biz.order_no": "1"}}),
    ("trace_stats", {"group_by": ["time", "path", "status"]}),
    ("trace_stats", {"group_by": ["time", "time"]}),
    ("trace_stats", {"group_by": ["day"]}),
    ("trace_stats", {"group_by": "time"}),
    ("trace_stats", {"group_by": ["time"], "interval": "2m"}),
    ("trace_stats", {"group_by": ["path"], "interval": "1m"}),
    ("trace_stats", {"interval": "1m"}),
    ("trace_stats", {"top": 0}),
    ("trace_stats", {"top": 101}),
    # 按 ID 取链路
    ("trace_get", {}),
    ("trace_get", {"trace_id": ""}),
    ("trace_get", {"trace_id": "g" * 32}),
    ("trace_get", {"trace_id": "a" * 33}),
    ("trace_get", {"trace_id": " " + TRACE_ID}),
    ("trace_get", {"trace_id": 123}),
    ("trace_get", {"trace_id": TRACE_ID, "max_nodes": 49}),
    ("trace_get", {"trace_id": TRACE_ID, "max_nodes": 401}),
    ("trace_get", {"trace_id": TRACE_ID, "around": "10:30"}),
    ("trace_get", {"trace_id": TRACE_ID, "focus_span": "a" * 17}),
    ("trace_get", {"trace_id": TRACE_ID, "focus_span": "xyz"}),
    # span_ids 和 errors=true 二选一
    ("trace_spans", {"trace_id": TRACE_ID}),
    ("trace_spans", {"trace_id": TRACE_ID, "errors": False}),
    ("trace_spans", {"trace_id": TRACE_ID, "errors": True, "span_ids": [SPAN_ID]}),
    ("trace_spans", {"trace_id": TRACE_ID, "span_ids": []}),
    ("trace_spans", {"trace_id": TRACE_ID, "span_ids": [SPAN_ID] * 21}),
    ("trace_spans", {"trace_id": TRACE_ID, "span_ids": ["zz"]}),
    ("trace_spans", {"trace_id": TRACE_ID, "span_ids": SPAN_ID}),
    ("trace_spans", {"trace_id": TRACE_ID, "errors": "true"}),
    ("trace_spans", {"span_ids": [SPAN_ID]}),
    ("trace_fields", {"service": ""}),
    ("trace_fields", {"service": 1}),
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
    """每个声明的参数都有校验分支：换成任何类型都不对的值时报参数错误（不是「没有校验规则」的 500）"""
    for field in EXTRA_SCHEMAS[operation][1]:
        args = {**copy.deepcopy(FULL[operation]), field: [[]]}
        with pytest.raises(AIError) as invalid:
            validate_arguments(operation, args)
        assert invalid.value.status == 400, (operation, field, str(invalid.value))


# ---- Skill ----

SKILL = SKILLS_ROOT / "kubedoor-trace" / "SKILL.md"


def test_trace_skill_is_registered():
    entry = next(item for item in SKILLS if item["id"] == "kubedoor-trace")
    assert entry["name"] == "链路追踪排查" and entry["description"]
    for item in SKILLS:
        text = (SKILLS_ROOT / item["id"] / "SKILL.md").read_text(encoding="utf-8")
        assert re.search(rf"^name: {re.escape(item['id'])}$", text, re.M)
    # DeepAgents 按 frontmatter 把 Skill 列进系统提示：名称要和目录一致，要有说明
    from deepagents.middleware.skills import _parse_skill_metadata
    meta = _parse_skill_metadata(SKILL.read_text(encoding="utf-8"), "/skills/kubedoor-trace/SKILL.md", "kubedoor-trace")
    assert meta["name"] == "kubedoor-trace" and "traceId" in meta["description"]


def test_trace_skill_covers_the_tools_and_the_troubleshooting_flow():
    text = SKILL.read_text(encoding="utf-8")
    # 模型用 read_file 读 Skill，结果会经过脱敏：不能有会被打掉的写法
    assert redact(text) == text
    for operation in ROUTES:
        assert f"`{operation}`" in text, operation
    # 引用 Jaeger 官方 Skill 的判读规则要注明出处
    for phrase in ("error-root-cause", "detect-n-plus-one", "jaegertracing/jaeger", "Apache-2.0"):
        assert phrase in text, phrase
    # 结合 KubeDoor 已有的工具
    for tool in ("logs", "previous_logs", "pods", "events", "event_history", "service_endpoints", "metrics",
                 "deployment_metrics", "kubedoor-code", "kubedoor-config", "runtime_config", "mysql", "redis", "switch_cluster"):
        assert f"`{tool}`" in text, tool
    for code in ("not_configured", "version_unsupported", "not_supported", "jaeger_unavailable", "clickhouse_unavailable"):
        assert f"`{code}`" in text, code
    for phrase in ("recovered", "serial=true", "同一个 Span", "around", "span_kind=\"server\"", "coverage", "scan.complete"):
        assert phrase in text, phrase
    # 例子里写成 `名字=值` 的参数都是目录里声明过的（source、outcome、serial、truncated 是结果里的字段）
    declared = set().union(*(set(EXTRA_SCHEMAS[operation][1]) for operation in ROUTES))
    used = set(re.findall(r"`([a-z_]+)=", text)) - {"source", "outcome", "serial", "truncated"}
    assert {"status_codes", "span_kind", "group_by", "contains"} <= used <= declared, used - declared


def test_k8s_skill_points_to_the_trace_skill():
    text = (SKILLS_ROOT / "kubedoor-k8s" / "references" / "tools.md").read_text(encoding="utf-8")
    assert "/skills/kubedoor-trace/SKILL.md" in text
    for operation in ROUTES:
        assert f"`{operation}`" in text, operation


HEADERS = {"X-Kubedoor-Token": "internal", "X-User-Name": "reader", "X-User-Permission": "read",
           "Accept": "application/json, text/event-stream"}


def decode(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    return response.json()


def test_http_and_mcp_serve_every_skill():
    fake = SimpleNamespace(bootstrap=AsyncMock(return_value={"clusters": []}))
    app = create_app(fake, token="internal")
    expected = SKILL.read_text(encoding="utf-8")
    with TestClient(app) as client:
        listed = client.get("/api/ai/skills", headers=HEADERS).json()["skills"]
        assert listed == SKILLS and "kubedoor-trace" in {item["id"] for item in listed}
        assert client.get("/api/ai/skills/kubedoor-trace", headers=HEADERS).json() == {"id": "kubedoor-trace", "content": expected}
        init = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        headers = {**HEADERS, "Mcp-Session-Id": init.headers["mcp-session-id"]}
        resources = decode(client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}}))
        assert {f"skills://{item['id']}" for item in SKILLS} <= {item["uri"] for item in resources["result"]["resources"]}
        read = decode(client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                                                                  "params": {"uri": "skills://kubedoor-trace"}}))
        assert read["result"]["contents"][0]["text"] == expected
