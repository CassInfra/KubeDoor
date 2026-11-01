"""获取运行配置和数据查询的网关部分。

- runtime_config：master 返回原样配置，网关打码后交给模型；master 拒绝（没开启、没权限、没配置、接口出错）原样交给
  模型；没指定服务时用页面选中的；要「查看运行配置」权限。
- mysql / redis / elasticsearch：建动作前校验（不合格不进审批）、占位符只能在凭证参数里、来源选择；占位符只在执行时
  换成原文（每个服务取一次全部配置，先核对连接的主机在这份配置里；发给 agent 的请求体里是原文，冻结的参数、事件里
  没有原文），结果里出现的原文（够长的）打码；direct 在本地执行。

kubedoor-tools 的 check_data_query / run_data_query 和数据查询的分类换成假的（data_tools），不依赖它的实现。
打码和主机匹配规则本身见 test_runtime_config.py。
"""
import copy
import json
import re
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from fakes import FakeStore
from kubedoor_ai import runtime_config as rc
from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.gateway import DIRECT_DATA_HINT, MIN_RESULT_SECRET_LENGTH, Gateway, PendingAction, RunContext
from kubedoor_ai.service import Service

ENV = "cluster-a"
CIPHER = CredentialCipher("ab" * 32)
POD = "order-7d9f8b6c5-x2x9k"
DB_PASSWORD = "Db#Pa55w0rd!"
REDIS_PASSWORD = "R3dis-Secret"
ES_PASSWORD = "Es-Pa55"
SENTINEL_PASSWORD = "Sentinel-Pw"
CONFIG = {
    "cluster": ENV, "namespace": "demo", "deployment": "deploy-order", "pod": POD, "container": "order",
    "image": "registry.example.com/team/order:v1", "lang": "java", "centers": ["config-server"], "basis": "runtime",
    "collected_at": "2026-11-01T10:00:00+08:00", "cached": True,
    "config": {"spring.datasource.url": "jdbc:mysql://db1.example.com:3306/order?useSSL=false",
               "spring.datasource.username": "order_ro", "spring.datasource.password": DB_PASSWORD,
               "spring.redis.host": "redis.example.com", "spring.redis.password": REDIS_PASSWORD},
    "env": {"ES_URL": f"http://elastic:{ES_PASSWORD}@es.example.com:9200", "JAVA_OPTS": "-Xmx1g"},
}
ORDER = {"namespace": "demo", "kind": "deployment", "name": "order"}
DB_SECRET = rc.placeholder(ORDER, "config:spring.datasource.password")
REDIS_SECRET = rc.placeholder(ORDER, "config:spring.redis.password")
ES_SECRET = rc.placeholder(ORDER, "env:ES_URL#password")
ONLINE = {"agent": {"available": True, "online": True, "ai_tools": True, "data_tools": True}}
OLD_AGENT = {"agent": {"available": True, "online": True, "ai_tools": True, "data_tools": False}}
OFFLINE = {"agent": {"available": False, "online": False, "ai_tools": True, "data_tools": True}}
QUERY = {"host": "db1.example.com", "port": 3306, "user": "order_ro", "password": DB_SECRET, "database": "order",
         "sql": "SELECT id, status FROM orders WHERE order_no = 'SO1' LIMIT 10"}


class Master:
    """假 master：/api/ai/internal/runtime-config 像外部接口一样按 keys 过滤后原样返回配置（含明文），configs 按
    Deployment / Pod 名给各个服务不同的配置；agent-execute 返回 agent 的查询结果。记下全部请求"""

    def __init__(self, config=CONFIG, refusal=None, configs=None):
        self.config, self.refusal, self.configs = config, refusal, configs or {}
        self.calls = []
        self.agent_result = {"success": True, "data": {"columns": ["note"], "rows": [[f"password is {DB_PASSWORD}"]],
                                                       "row_count": 1, "truncated": False}, "truncated": False}

    def handle(self, request):
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, params, body))
        if request.url.path == "/api/ai/internal/runtime-config":
            if self.refusal:
                return httpx.Response(self.refusal[0], json=self.refusal[1])
            config = copy.deepcopy(self.configs.get(params.get("deployment") or params.get("pod"), self.config))
            if params.get("keys"):
                pattern = re.compile(params["keys"], re.I)
                for section in ("config", "env"):
                    config[section] = {k: v for k, v in config.get(section, {}).items() if pattern.search(k)}
            request_body = {key: params[key] for key in ("namespace", "deployment", "pod", "container", "keys") if key in params}
            return httpx.Response(200, json={"success": True, "data": {"request": {"k8s": params["env"], **request_body}, "config": config}})
        if request.url.path == "/api/ai/internal/agent-execute":
            return httpx.Response(200, json=self.agent_result)
        return httpx.Response(404, json={"success": False, "error": "not found"})

    def paths(self):
        return [path for path, _, _ in self.calls]

    def resolves(self):
        return [params for path, params, _ in self.calls if path == "/api/ai/internal/runtime-config"]

    def executed(self):
        return [body for path, _, body in self.calls if path == "/api/ai/internal/agent-execute"]


@pytest.fixture
def data_tools(monkeypatch):
    """kubedoor-tools 的数据查询换成假的：reason 是 check_data_query 的返回（None 合格），result 是 run_data_query 的返回"""
    import kubedoor_tools
    state = SimpleNamespace(checked=[], ran=[], reason=None,
                            result={"success": True, "data": {"columns": ["id"], "rows": [[1]], "row_count": 1, "truncated": False},
                                    "truncated": False})
    original = kubedoor_tools.classify_operation

    def check(operation, arguments):
        state.checked.append((operation, copy.deepcopy(arguments)))
        return state.reason

    async def run(operation, arguments, timeout=60):
        state.ran.append((operation, copy.deepcopy(arguments), timeout))
        return copy.deepcopy(state.result)

    def classify(operation, arguments, commands=None):
        if operation in ("mysql", "redis", "elasticsearch"):
            return {"read_only": state.reason is None, "reason": state.reason or "只读查询", "ops": ["data.query"],
                    "category": None, "dangerous": False}
        return original(operation, arguments, commands)

    monkeypatch.setattr(kubedoor_tools, "check_data_query", check, raising=False)
    monkeypatch.setattr(kubedoor_tools, "run_data_query", run, raising=False)
    monkeypatch.setattr(kubedoor_tools, "classify_operation", classify)
    return state


def context(permission="rw", capabilities=ONLINE, scope=None, clusters=None):
    return RunContext({"id": "run-1", "session_id": "session-1", "origin": "chat", "skill_ids": [],
                       "scope": scope or {"env": ENV, "namespace": "demo", "deployment": None, "pod": None},
                       "capabilities": capabilities},
                      Identity("operator", permission, MappingProxyType(clusters or {})))


def make_gateway(master):
    return Gateway(FakeStore(), CIPHER, "http://master", "internal-token",
                   http=httpx.AsyncClient(transport=httpx.MockTransport(master.handle)))


def dumped(value):
    return json.dumps(value, ensure_ascii=False, default=str)


# ---- runtime_config ----

@pytest.mark.asyncio
async def test_runtime_config_masks_the_config_before_the_model_sees_it():
    master = Master()
    gateway = make_gateway(master)
    try:
        result = await gateway.invoke(context(), "runtime_config", {
            "namespace": "demo", "deployment": "order", "container": "order", "keys": "datasource|redis|ES_",
            "detail": True, "refresh": True}, call_id="cfg", mcp=True)
    finally:
        await gateway.close()
    [(path, params, _)] = master.calls
    assert path == "/api/ai/internal/runtime-config"
    assert params == {"env": ENV, "namespace": "demo", "deployment": "order", "container": "order", "purpose": "view",
                      "keys": "datasource|redis|ES_", "detail": "1", "refresh": "1"}
    assert result["success"] is True and result["source"] == "kubedoor" and result["freshness"] == "live"
    target = {**ORDER, "container": "order"}
    view = result["data"]
    config, env = dict(view["config"]), dict(view["env"])
    assert config["spring.datasource.password"] == rc.placeholder(target, "config:spring.datasource.password")
    assert config["spring.datasource.username"] == "order_ro" and "JAVA_OPTS" not in env
    assert env["ES_URL"] == f"http://elastic:{rc.placeholder(target, 'env:ES_URL#password')}@es.example.com:9200"
    assert view["service"]["pod"] == POD and len(view["secret_refs"]) == 3 and result["truncated"] is False
    # 交给模型的、存进动作和事件的都没有明文
    for secret in (DB_PASSWORD, REDIS_PASSWORD, ES_PASSWORD):
        assert secret not in dumped(result) and secret not in dumped(gateway.store.actions) and secret not in dumped(gateway.store.events)
    [action] = gateway.store.actions.values()
    assert (action["state"], action["ops"], action["read_only"]) == ("succeeded", ["config.view"], True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, code, message", [
    (403, "ai_config_disabled", "系统管理员没有开启「允许 AI 获取运行配置」（系统管理 → 获取运行配置接口）"),
    (403, "permission_denied", "当前账号在集群 cluster-a 不能「查看运行配置」"),
    (400, "not_configured", "系统管理员还没有配置获取运行配置接口（系统管理 → 获取运行配置接口）"),
    (502, "upstream_error", "获取运行配置接口出错：HTTP 404 没有集群 cluster-a"),
    (504, "timeout", "获取运行配置接口 60 秒内没有采集完，请稍后再试"),
])
async def test_master_refusals_are_passed_to_the_model(status, code, message):
    master = Master(refusal=(status, {"success": False, "code": code, "message": message, "error": message}))
    gateway = make_gateway(master)
    try:
        result = await gateway.invoke(context(), "runtime_config", {"namespace": "demo", "pod": POD}, call_id="cfg", mcp=True)
    finally:
        await gateway.close()
    assert master.calls[0][1]["pod"] == POD and "deployment" not in master.calls[0][1]
    assert result["success"] is False and result["error"] == {"code": code, "message": message}
    assert next(iter(gateway.store.actions.values()))["state"] == "failed"


@pytest.mark.asyncio
async def test_runtime_config_defaults_to_selected_pod_then_deployment():
    selected = {"env": ENV, "namespace": "demo", "deployment": {"namespace": "demo", "name": "order"},
                "pod": {"namespace": "demo", "name": POD}}
    cases = [
        (selected, {}, {"namespace": "demo", "pod": POD}),
        ({**selected, "pod": None}, {"keys": "redis"}, {"namespace": "demo", "deployment": "order", "keys": "redis"}),
        (selected, {"deployment": "other"}, {"namespace": "demo", "deployment": "other"}),   # 指定了就用指定的
    ]
    for index, (scope, arguments, expected) in enumerate(cases):
        gateway = make_gateway(Master())
        gateway.execute_action = AsyncMock(return_value={"success": True})
        try:
            await gateway.invoke(context(scope=scope), "runtime_config", arguments, call_id=f"cfg-{index}", mcp=True)
        finally:
            await gateway.close()
        assert gateway.execute_action.await_args.args[1]["arguments"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments, message", [
    ({"namespace": "demo"}, "二选一"),
    ({"namespace": "demo", "deployment": "order", "pod": POD}, "二选一"),
    ({"namespace": "Demo", "deployment": "order"}, "namespace 不是合法的 K8S 名称"),
    ({"namespace": "demo", "pod": "../x"}, "pod 不是合法的 K8S 名称"),
    ({"namespace": "demo", "deployment": "order", "container": "a_b"}, "container 不是合法的 K8S 名称"),
    ({"namespace": "demo", "deployment": "order", "keys": "a" * 501}, "keys 最多 500 个字符"),
    ({"namespace": "demo", "deployment": "order", "keys": "a\nb"}, "控制字符"),
    ({"namespace": "demo", "deployment": "order", "detail": "yes"}, "detail 必须为 boolean"),
])
async def test_invalid_runtime_config_calls_fail_before_any_action(arguments, message):
    master = Master()
    gateway = make_gateway(master)
    try:
        with pytest.raises(AIError, match=message):
            await gateway.invoke(context(), "runtime_config", arguments, call_id="bad", mcp=True)
    finally:
        await gateway.close()
    assert gateway.store.actions == {} and master.calls == []


@pytest.mark.asyncio
async def test_runtime_config_needs_config_view(permission_policy):
    master = Master()
    gateway = make_gateway(master)
    arguments = {"namespace": "demo", "deployment": "order"}
    try:
        # 出厂只读不能看运行配置：不建动作、不问 master
        with pytest.raises(AIError) as denied:
            await gateway.invoke(context("read"), "runtime_config", arguments, call_id="read", mcp=True)
        assert (denied.value.status, denied.value.code) == (403, "permission_denied")
        assert "「查看运行配置」" in str(denied.value) and gateway.store.actions == {} and master.calls == []
        # 单独授权为读写的集群可以，直接执行
        assert (await gateway.invoke(context("read", clusters={ENV: "rw"}), "runtime_config", arguments, call_id="rw", mcp=True))["success"]
        # 管理员可以给它打开审批
        permission_policy.policy["ops"]["config.view"]["approval"] = True
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context(), "runtime_config", arguments, call_id="approve", mcp=True)
        assert pending.value.action["ops"] == ["config.view"]
        assert len(master.calls) == 1
    finally:
        await gateway.close()


# ---- 数据查询：建动作之前 ----

@pytest.mark.asyncio
async def test_invalid_query_is_refused_before_any_action(data_tools):
    data_tools.reason = "只允许一条 SELECT / SHOW / DESC / EXPLAIN 语句"
    master = Master()
    gateway = make_gateway(master)
    try:
        with pytest.raises(AIError) as invalid:
            await gateway.invoke(context(), "mysql", {**QUERY, "sql": "DELETE FROM orders"}, call_id="bad", mcp=True)
    finally:
        await gateway.close()
    assert (invalid.value.status, invalid.value.code, str(invalid.value)) == (400, "invalid_query", data_tools.reason)
    assert data_tools.checked == [("mysql", {**QUERY, "sql": "DELETE FROM orders"})]
    assert gateway.store.actions == {} and master.calls == [] and data_tools.ran == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, arguments, field", [
    ("mysql", {**QUERY, "sql": f"SELECT ASCII(SUBSTR('{DB_SECRET}', 1, 1))"}, "sql"),
    ("mysql", {**QUERY, "host": DB_SECRET}, "host"),
    ("redis", {"host": "r", "password": DB_SECRET, "command": ["GET", DB_SECRET]}, "command"),
    # 哨兵模式：哨兵自己的密码 sentinel_password 可以用占位符，主节点名、哨兵用户名不行
    ("redis", {"host": "r", "sentinel_password": DB_SECRET, "sentinel_master": DB_SECRET, "command": ["PING"]}, "sentinel_master"),
    ("redis", {"host": "r", "sentinel_username": DB_SECRET, "sentinel_password": DB_SECRET, "command": ["PING"]}, "sentinel_username"),
    ("elasticsearch", {"url": "http://es:9200", "api_key": DB_SECRET, "path": "/_search", "body": {"q": DB_SECRET}}, "body"),
])
async def test_placeholders_only_go_into_credential_fields(data_tools, operation, arguments, field):
    """写进 SQL、命令、body 的占位符会被换成原文，结果里就能带出密码的长度、逐个字符：建动作前拒绝"""
    master = Master()
    gateway = make_gateway(master)
    try:
        with pytest.raises(AIError) as invalid:
            await gateway.invoke(context(), operation, arguments, call_id="bad", mcp=True)
    finally:
        await gateway.close()
    assert invalid.value.code == "invalid_query" and str(invalid.value).endswith(f"不能出现在 {field} 里")
    assert f"只能原样填在 {'、'.join(rc.SECRET_FIELDS[operation])} 参数里" in str(invalid.value)
    assert gateway.store.actions == {} and master.calls == []


@pytest.mark.asyncio
async def test_source_selection_for_data_queries(data_tools, permission_policy):
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    plain = {**QUERY, "password": "typed-by-user"}   # 用户自己给的密码，不用解析
    cases = [(ONLINE, "auto", "agent"), (OLD_AGENT, "auto", "direct"), (OFFLINE, "auto", "direct"),
             (ONLINE, "direct", "direct"), ({}, "auto", "direct")]
    for index, (capabilities, source, expected) in enumerate(cases):
        master = Master()
        gateway = make_gateway(master)
        try:
            result = await gateway.invoke(context(capabilities=capabilities), "mysql", plain, source, call_id=f"q{index}", mcp=True)
        finally:
            await gateway.close()
        [action] = gateway.store.actions.values()
        assert (action["source"], action["state"], result["source"]) == (expected, "succeeded", expected), (capabilities, source)
        assert master.paths() == (["/api/ai/internal/agent-execute"] if expected == "agent" else [])
    assert len(data_tools.ran) == 4
    # 指定 agent 但它没升级或不在线：建动作前就说清楚
    for capabilities in (OLD_AGENT, OFFLINE):
        gateway = make_gateway(Master())
        try:
            with pytest.raises(AIError) as unsupported:
                await gateway.invoke(context(capabilities=capabilities), "redis", {"host": "r", "command": ["PING"]}, "agent", call_id="a", mcp=True)
        finally:
            await gateway.close()
        assert (unsupported.value.status, unsupported.value.code) == (409, "agent_unsupported") and gateway.store.actions == {}
    gateway = make_gateway(Master())
    try:
        with pytest.raises(AIError, match="没有这个 KubeDoor 目录工具"):
            await gateway.invoke(context(), "mysql", plain, "kubedoor", call_id="k", mcp=True)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_data_query_needs_permission_and_approval_by_default(data_tools, permission_policy):
    master = Master()
    gateway = make_gateway(master)
    try:
        with pytest.raises(AIError) as denied:
            await gateway.invoke(context("read"), "mysql", QUERY, call_id="read", mcp=True)
        assert denied.value.code == "permission_denied" and "「AI 查询数据库 / Redis / ES」" in str(denied.value)
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context(), "mysql", QUERY, call_id="rw", mcp=True)
        action = gateway.store.actions[pending.value.action["action_id"]]
        assert (action["ops"], action["read_only"], action["approval_fixed"]) == (["data.query"], True, False)
        # 审批期间被降成只读：执行前复核拦下，不解析占位符
        result = await gateway.dispatch(context("rw", clusters={ENV: "read"}), action)
        assert result["error"]["code"] == "permission_denied" and master.calls == []
    finally:
        await gateway.close()


# ---- 数据查询：执行时才把占位符换成原文 ----

@pytest.mark.asyncio
async def test_placeholders_are_resolved_only_when_executing(data_tools):
    master = Master()
    gateway = make_gateway(master)
    ctx = context()
    try:
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(ctx, "mysql", QUERY, call_id="q", mcp=True)
        # 批准前：冻结的参数、审批卡片里只有占位符，没去取配置
        action = gateway.store.actions[pending.value.action["action_id"]]
        assert action["arguments"] == QUERY and master.calls == [] and DB_PASSWORD not in dumped(pending.value.action)
        result = await gateway.dispatch(ctx, action)
    finally:
        await gateway.close()
    # 执行时按占位符里的服务重新取全部配置（不按 keys 过滤），再把原文发给 agent
    assert master.paths() == ["/api/ai/internal/runtime-config", "/api/ai/internal/agent-execute"]
    assert master.calls[0][1] == {"env": ENV, "namespace": "demo", "deployment": "order", "purpose": "resolve"}
    [sent] = master.executed()
    assert sent["arguments"] == {**QUERY, "password": DB_PASSWORD}
    assert (sent["operation"], sent["phase"], sent["approved"], sent["ops"]) == ("mysql", "execute", True, ["data.query"])
    # 冻结的参数仍是占位符；结果、事件里的原文都打码
    stored = gateway.store.actions[action["id"]]
    assert stored["arguments"]["password"] == DB_SECRET and stored["state"] == "succeeded"
    assert result["success"] is True and result["data"]["rows"] == [["password is [redacted]"]]
    assert DB_PASSWORD in ctx.secrets
    for value in (result, stored["result"], gateway.store.events):
        assert DB_PASSWORD not in dumped(value)
    start = next(event["data"] for event in gateway.store.events if event["type"] == "tool_start")
    assert start["arguments"]["sql"] == QUERY["sql"] and start["arguments"]["password"] != DB_PASSWORD


@pytest.mark.asyncio
async def test_direct_queries_run_in_kubedoor_ai(data_tools, permission_policy):
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    master = Master()
    gateway = make_gateway(master)
    arguments = {"url": "http://es.example.com:9200", "username": "elastic", "password": ES_SECRET, "path": "/_cat/indices"}
    try:
        result = await gateway.invoke(context(capabilities=OLD_AGENT), "elasticsearch", arguments, call_id="es", mcp=True)
        # 连不上时提示业务库通常只有业务集群里能连上，请升级 agent；参数错误不加
        data_tools.result = {"success": False, "error": {"code": "CONNECT_FAILED", "message": f"连接失败 {ES_PASSWORD}"}}
        failed = await gateway.invoke(context(capabilities=OLD_AGENT), "elasticsearch", arguments, call_id="es-2", mcp=True)
        data_tools.result = {"success": False, "error": {"code": "INVALID_ARGUMENT", "message": "path 不支持"}}
        invalid = await gateway.invoke(context(capabilities=OLD_AGENT), "elasticsearch", arguments, call_id="es-3", mcp=True)
    finally:
        await gateway.close()
    # 不需要 kubeconfig，不经 agent；本地函数收到的是原文
    assert result["success"] is True and result["source"] == "direct"
    assert [call[:2] for call in data_tools.ran] == [("elasticsearch", {**arguments, "password": ES_PASSWORD})] * 3
    assert "/api/ai/internal/agent-execute" not in master.paths()
    assert failed["hint"] == DIRECT_DATA_HINT and ES_PASSWORD not in dumped(failed)
    assert failed["error"]["message"] == "连接失败 [redacted]" and "hint" not in invalid


@pytest.mark.asyncio
async def test_placeholders_from_several_services_inside_a_string(data_tools, permission_policy):
    """占位符可以嵌在字符串中间；不同服务各取一次配置"""
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    pod = {"namespace": "demo", "kind": "pod", "name": POD, "container": "order"}
    redis_secret = rc.placeholder(pod, "config:spring.redis.password")
    master = Master()
    gateway = make_gateway(master)
    try:
        await gateway.invoke(context(), "redis", {"host": "redis.example.com", "password": f"{DB_SECRET}:{redis_secret}",
                                                  "command": ["PING"]}, call_id="r", mcp=True)
    finally:
        await gateway.close()
    resolves = [params for path, params, _ in master.calls if path == "/api/ai/internal/runtime-config"]
    assert [{key: params.get(key) for key in ("deployment", "pod", "container")} for params in resolves] == [
        {"deployment": "order", "pod": None, "container": None}, {"deployment": None, "pod": POD, "container": "order"}]
    assert master.executed()[0]["arguments"]["password"] == f"{DB_PASSWORD}:{REDIS_PASSWORD}"


SENTINEL_CONFIG = {**CONFIG, "config": {**CONFIG["config"], "spring.redis.sentinel.master": "mymaster",
                                        "spring.redis.sentinel.nodes": "10.0.0.41:26379,10.0.0.42:26379",
                                        "spring.redis.sentinel.password": SENTINEL_PASSWORD}}


@pytest.mark.asyncio
async def test_resolve_fetches_the_whole_config_once_per_service(data_tools, permission_policy):
    """核对连接地址要用全部配置：同一个服务的几个占位符只取一次配置、不带 keys。Redis 哨兵模式下 host 是配置里的
    哨兵地址，哨兵自己的密码填 sentinel_password"""
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    master = Master(config=SENTINEL_CONFIG)
    gateway = make_gateway(master)
    sentinel_secret = rc.placeholder(ORDER, "config:spring.redis.sentinel.password")
    arguments = {"host": "10.0.0.42", "port": 26379, "sentinel_master": "mymaster", "password": REDIS_SECRET,
                 "sentinel_password": sentinel_secret, "command": ["GET", "order:1"]}
    try:
        result = await gateway.invoke(context(), "redis", arguments, call_id="r", mcp=True)
    finally:
        await gateway.close()
    assert master.resolves() == [{"env": ENV, "namespace": "demo", "deployment": "order", "purpose": "resolve"}]
    assert result["success"] is True
    assert master.executed()[0]["arguments"] == {**arguments, "password": REDIS_PASSWORD, "sentinel_password": SENTINEL_PASSWORD}


# 配置文件里也写了地址；10.0.0.12 只出现在文件内容里
HOST_CONFIG = {**CONFIG, "files": [{"path": "/app/conf/cache.properties", "from": "镜像", "reader": "java",
                                    "content": "# 本地缓存\ncache.host=10.0.0.12\ncache.port=6380\n"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, arguments, allowed", [
    ("mysql", QUERY, True),                                                          # 在配置值（JDBC URL）里
    ("mysql", {**QUERY, "host": " DB1.Example.COM ", "port": 3307}, True),           # 不区分大小写，端口不比较
    ("redis", {"host": "10.0.0.12", "password": REDIS_SECRET, "command": ["PING"]}, True),   # 在配置文件内容里
    ("elasticsearch", {"url": "https://ES.example.com:9243/prefix", "username": "elastic", "password": ES_SECRET,
                       "path": "/_cat/indices"}, True),                              # ES 看 url 的主机名
    ("mysql", {**QUERY, "host": "evil.example.net"}, False),                         # 不在配置里
    ("mysql", {**QUERY, "host": "10.0.0.5"}, False),                                 # 配置里写的是域名，填 IP 不行
    ("redis", {"host": "cache.example.com", "password": REDIS_SECRET, "command": ["PING"]}, False),   # 配置里是 IP
    ("mysql", {**QUERY, "host": "example.com"}, False),                              # 只是 db1.example.com 的后缀
    ("redis", {"host": "10.0.0.1", "password": REDIS_SECRET, "command": ["PING"]}, False),           # 只是 10.0.0.12 的前缀
    ("elasticsearch", {"url": "http://es.example.com.evil.net:9200", "username": "elastic", "password": ES_SECRET,
                       "path": "/_search"}, False),
])
async def test_connection_host_must_appear_in_the_service_config(data_tools, permission_policy, operation, arguments, allowed):
    """用了占位符时，配置里的密码只能连这个服务自己配置的地址；agent、direct 两种来源都在执行前核对"""
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    for index, capabilities in enumerate((ONLINE, OLD_AGENT)):
        master = Master(config=HOST_CONFIG)
        gateway = make_gateway(master)
        try:
            result = await gateway.invoke(context(capabilities=capabilities), operation, arguments, call_id=f"q{index}", mcp=True)
        finally:
            await gateway.close()
        assert len(master.resolves()) == 1
        [action] = gateway.store.actions.values()
        if allowed:
            assert result["success"] is True and action["state"] == "succeeded"
            continue
        host = rc.connection_host(operation, arguments)
        assert result["success"] is False and result["error"]["code"] == "secret_host_mismatch"
        assert f"连接地址 {host} 没有出现在服务 demo/deployment/order 的运行配置里" in result["error"]["message"]
        assert action["state"] == "failed" and master.executed() == []
    if not allowed:
        assert data_tools.ran == []


@pytest.mark.asyncio
async def test_every_service_must_configure_the_host(data_tools, permission_policy):
    """占位符来自几个服务时，每个服务的配置里都要有这个地址"""
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    pod = {"namespace": "demo", "kind": "pod", "name": POD, "container": "order"}
    # Pod 的配置里只有 other-redis.example.com（- 前缀不算 redis.example.com）
    other = {**CONFIG, "config": {"spring.redis.host": "other-redis.example.com", "spring.redis.password": REDIS_PASSWORD}}
    master = Master(configs={POD: other})
    gateway = make_gateway(master)
    arguments = {"host": "redis.example.com", "password": f"{DB_SECRET}:{rc.placeholder(pod, 'config:spring.redis.password')}",
                 "command": ["PING"]}
    try:
        result = await gateway.invoke(context(), "redis", arguments, call_id="r", mcp=True)
    finally:
        await gateway.close()
    assert result["error"]["code"] == "secret_host_mismatch" and f"服务 demo/pod/{POD}/order 的运行配置" in result["error"]["message"]
    assert len(master.resolves()) == 2 and master.executed() == []


@pytest.mark.asyncio
async def test_host_is_not_checked_without_placeholders(data_tools, permission_policy):
    """用户自己给的密码不涉及配置里的密码：不取配置、不核对地址"""
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    master = Master()
    gateway = make_gateway(master)
    try:
        result = await gateway.invoke(context(), "mysql", {**QUERY, "host": "10.9.9.9", "password": "typed-by-user"}, call_id="q", mcp=True)
    finally:
        await gateway.close()
    assert result["success"] is True and master.resolves() == [] and len(master.executed()) == 1


@pytest.mark.asyncio
async def test_only_long_enough_secrets_are_masked_in_results(data_tools, permission_policy):
    """原文按子串打码结果：短于 MIN_RESULT_SECRET_LENGTH 的（root）照样替换、但不追加进 ctx.secrets，免得结果里所有的
    root 都被打掉"""
    assert MIN_RESULT_SECRET_LENGTH == 6
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    short = {**CONFIG, "config": {**CONFIG["config"], "spring.redis.password": "root", "spring.datasource.password": "Pw-123"}}
    master = Master(config=short)
    master.agent_result = {"success": True, "data": {"value": "root:x:0:0:root:/root:/bin/sh"}, "truncated": False}
    gateway = make_gateway(master)
    ctx = context()
    try:
        result = await gateway.invoke(ctx, "redis", {"host": "redis.example.com", "password": REDIS_SECRET,
                                                     "command": ["GET", "passwd"]}, call_id="short", mcp=True)
        assert master.executed()[0]["arguments"]["password"] == "root"
        assert "root" not in ctx.secrets and result["data"]["value"] == "root:x:0:0:root:/root:/bin/sh"
        await gateway.invoke(ctx, "mysql", QUERY, call_id="six", mcp=True)   # 正好 6 个字符的要打码
    finally:
        await gateway.close()
    assert master.executed()[1]["arguments"]["password"] == "Pw-123"
    assert "Pw-123" in ctx.secrets and "root" not in ctx.secrets


@pytest.mark.asyncio
async def test_unresolvable_placeholders_are_not_executed(data_tools, permission_policy):
    permission_policy.policy["ops"]["data.query"]["approval"] = False
    gone = rc.placeholder(ORDER, "config:spring.datasource.old-password")
    cases = [
        (Master(), {**QUERY, "password": gone}, "secret_not_found", "配置可能已变化"),
        (Master(), {**QUERY, "password": "<secret:demo/statefulset/x:config:a>"}, "secret_invalid", "原样复制"),
        # 管理员关掉了「允许 AI 获取运行配置」、账号没有「查看运行配置」：配置里的密码也不能用了
        (Master(refusal=(403, {"success": False, "code": "ai_config_disabled", "message": "没有开启"})), QUERY, "ai_config_disabled", "没有开启"),
        (Master(refusal=(403, {"success": False, "code": "permission_denied", "message": "不能查看运行配置"})), QUERY, "permission_denied", "不能查看运行配置"),
    ]
    for index, (master, arguments, code, message) in enumerate(cases):
        gateway = make_gateway(master)
        try:
            result = await gateway.invoke(context(), "mysql", arguments, call_id=f"q{index}", mcp=True)
        finally:
            await gateway.close()
        assert result["success"] is False and result["error"]["code"] == code and message in result["error"]["message"]
        assert master.executed() == [] and next(iter(gateway.store.actions.values()))["state"] == "failed"
    assert data_tools.ran == []


# ---- 来源能力与审批 ----

def test_capabilities_report_agent_data_tools():
    assert Service.capabilities({"online": True, "ai_tools": True, "data_tools": True})["agent"] == {
        "available": True, "online": True, "ai_tools": True, "data_tools": True}
    assert Service.capabilities({"online": True, "ai_tools": True})["agent"]["data_tools"] is False


class DecisionStore(FakeStore):
    def __init__(self, run):
        super().__init__()
        self.run_row = run

    async def run(self, rid, identity):
        return dict(self.run_row)


@pytest.mark.asyncio
async def test_approving_a_direct_query_does_not_need_kubeconfig():
    store = DecisionStore({"id": "run-1", "session_id": "session-1", "username": "operator", "status": "waiting_approval",
                           "cancel_requested": False, "origin": "chat", "scope": {"env": ENV, "namespace": "demo"}})
    store.actions["a-1"] = {"id": "a-1", "run_id": "run-1", "call_id": "run-1:c", "operation": "mysql", "source": "direct",
                            "arguments": QUERY, "preview": {"operation": "mysql", "arguments": QUERY}, "read_only": True,
                            "connection_revision": None, "state": "pending", "interrupt_id": "interrupt-1", "result": None,
                            "expires_at": "2099-01-01T00:00:00+00:00", "ops": ["data.query"], "approval_fixed": False}
    gateway = Gateway(store, CIPHER, "http://master", "internal")
    runtime = SimpleNamespace(tasks={}, contexts={}, start=Mock())
    service = Service(store, gateway, runtime)
    service.ensure_env = AsyncMock(return_value={"env": ENV, "online": True, "ai_tools": True, "data_tools": False})
    body = {"action_id": "a-1", "decision": "approve", "provider": {"base_url": "http://model/v1", "api_key": "k", "model": "m"}}
    try:
        assert (await service.decision(Identity("operator", "rw"), "run-1", body))["status"] == "running"
    finally:
        await gateway.close()
    runtime.start.assert_called_once()
    assert runtime.start.call_args.args[0]["capabilities"]["agent"]["data_tools"] is False
