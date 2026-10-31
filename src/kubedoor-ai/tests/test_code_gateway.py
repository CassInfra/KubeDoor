"""源码分析的网关部分：下载地址只从 master 取、每次搜索 / 读取前问 master、code_id 绑定集群、选中资源做默认值。
下载、解压、搜索本身见 test_code.py。"""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from fakes import FakeStore
from kubedoor_ai.code import CodeError
from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.gateway import Gateway, RunContext


ENV = "cluster-a"
POD = "app-7d9f8b6c5-x2x9k"
IMAGE = "registry.example.com/team/app:release-9f9a76c5"
CODE_ID = "0123456789abcdef"
REQUEST = {"k8s": ENV, "namespace": "ops", "deployment": "app", "image": IMAGE}
LINKS = {
    "web_url": "https://gitlab.example.com/team/app/tree/9f9a76c5",
    "download_url": "https://code.example.com/archive/app-9f9a76c5.tar.gz?expires=1793543448&sig=ac17",
    "commits_url": "https://code.example.com/commits/app?expires=1793543448&sig=ac17",
}
DISABLED = "系统管理员没有开启「允许 AI 下载并分析源码」"
SUMMARY = {"code_id": CODE_ID, "cached": False, "file_count": 3, "bytes": 120, "skipped_files": 0,
           "top": ["pom.xml", "src/"], "languages": {"java": 2}, "markers": ["pom.xml"]}


class FakeCode:
    """CodeCache 的替身：记下调用，按需返回 meta 或抛 CodeError"""

    def __init__(self, meta=None, error=None):
        self.calls, self._meta, self.error = [], meta, error

    async def fetch(self, **kwargs):
        self.calls.append(("fetch", kwargs))
        if self.error:
            raise self.error
        return dict(SUMMARY)

    def meta(self, code_id):
        self.calls.append(("meta", code_id))
        return self._meta

    async def search(self, code_id, **kwargs):
        self.calls.append(("search", code_id, kwargs))
        if self.error:
            raise self.error
        return {"code_id": code_id, "matches": [{"path": "src/A.java", "line": 3, "text": "throw new X();"}],
                "count": 1, "truncated": False}

    async def read(self, code_id, path, **kwargs):
        self.calls.append(("read", code_id, path, kwargs))
        return {"code_id": code_id, "path": path, "start_line": 1, "end_line": 1, "total_lines": 9,
                "content": "1\tclass A {\n", "truncated": True}

    async def symbols(self, code_id, **kwargs):
        self.calls.append(("symbols", code_id, kwargs))
        return {"code_id": code_id, "name": kwargs.get("name", ""), "count": 1, "truncated": False, "scanned": 1,
                "symbols": [{"name": "create", "kind": "method", "path": "src/A.java", "line": 3, "end": 9,
                             "scope": "A", "scope_kind": "class", "signature": "()"}]}

    async def cleanup(self):
        self.calls.append(("cleanup",))


def context(permission="read", scope=None):
    return RunContext({"id": "run-1", "session_id": "session-1",
                       "scope": scope or {"env": ENV, "namespace": "", "deployment": None, "pod": None}},
                      Identity("user", permission))


def gateway_for(responses, code):
    """responses: 按顺序返回给 master 请求的 (status, json)；记下收到的请求"""
    seen = []

    def receive(request):
        seen.append(request)
        status, body = responses[len(seen) - 1]
        return httpx.Response(status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    return Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "internal-token", http=client, code=code), seen


def action(operation, arguments):
    return {"source": "kubedoor", "operation": operation, "arguments": arguments}


META = {"code_id": CODE_ID, "env": ENV, "namespace": "ops", "deployment": "app", "image": IMAGE}


@pytest.mark.asyncio
async def test_code_fetch_takes_download_url_from_master_only():
    code = FakeCode()
    data = {"request": REQUEST, "links": LINKS, "invalid": {}, "pod": POD, "tls_verify": False}
    gateway, seen = gateway_for([(200, {"success": True, "data": data})], code)
    try:
        # 模型塞进来的地址和其他集群都不会被采用
        result = await gateway.execute_action(context(), action("code_fetch", {
            "namespace": "ops", "pod": POD, "download_url": "http://evil.example.com/x.tgz", "url": "http://evil"}))
    finally:
        await gateway.close()
    [request] = seen
    assert request.url.path == "/api/ai/internal/code-info"
    assert dict(request.url.params) == {"env": ENV, "namespace": "ops", "pod": POD, "purpose": "fetch"}
    assert request.headers["X-User-Name"] == "user"
    assert code.calls == [("fetch", {"env": ENV, "namespace": "ops", "deployment": "app", "image": IMAGE,
                                     "download_url": LINKS["download_url"], "tls_verify": False})]
    assert result["success"] is True and result["freshness"] == "live" and result["source"] == "kubedoor"
    assert result["data"] == {**SUMMARY, "service": {"namespace": "ops", "deployment": "app", "image": IMAGE},
                              "pod": POD, "links": {"web_url": LINKS["web_url"], "commits_url": LINKS["commits_url"]}}
    # 下载地址（带签名）不交给模型
    assert "download_url" not in json.dumps(result) and LINKS["download_url"] not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, body, code", [
    (403, {"success": False, "code": "ai_download_disabled", "message": DISABLED, "error": DISABLED}, "ai_download_disabled"),
    (400, {"success": False, "code": "not_configured", "message": "还没有配置", "error": "还没有配置"}, "not_configured"),
    (502, {"success": False, "code": "upstream_error", "message": "获取代码接口出错:HTTP 404", "error": "获取代码接口出错:HTTP 404"}, "upstream_error"),
])
async def test_code_fetch_reports_master_refusal(status, body, code):
    fake = FakeCode()
    gateway, _ = gateway_for([(status, body)], fake)
    try:
        result = await gateway.execute_action(context(), action("code_fetch", {"namespace": "ops", "deployment": "app"}))
    finally:
        await gateway.close()
    assert result == {"success": False, "error": {"code": code, "message": body["message"]}}
    assert fake.calls == []


@pytest.mark.asyncio
async def test_code_fetch_without_download_url_and_cache_errors():
    data = {"request": REQUEST, "links": {**LINKS, "download_url": ""}, "invalid": {}}
    fake = FakeCode()
    gateway, _ = gateway_for([(200, {"success": True, "data": data})], fake)
    try:
        missing = await gateway.execute_action(context(), action("code_fetch", {"namespace": "ops", "deployment": "app"}))
    finally:
        await gateway.close()
    assert missing["success"] is False and missing["error"]["code"] == "no_download_url" and fake.calls == []

    broken = FakeCode(error=CodeError("压缩包超过 300MB", "archive_too_large"))
    gateway, _ = gateway_for([(200, {"success": True, "data": {**data, "links": LINKS}})], broken)
    try:
        result = await gateway.execute_action(context(), action("code_fetch", {"namespace": "ops", "deployment": "app"}))
    finally:
        await gateway.close()
    assert result == {"success": False, "error": {"code": "archive_too_large", "message": "压缩包超过 300MB"}}


@pytest.mark.asyncio
async def test_code_search_checks_cluster_and_access_before_searching():
    args = {"code_id": CODE_ID, "pattern": "throw new", "glob": "**/*.java", "fixed_strings": True, "context": 2,
            "max_results": 20, "namespace": "ops"}
    # 不认识的 code_id、别的集群的源码：不问 master，直接拒绝
    for meta, code in ((None, "code_not_found"), ({**META, "env": "cluster-b"}, "scope_violation")):
        fake = FakeCode(meta)
        gateway, seen = gateway_for([], fake)
        try:
            result = await gateway.execute_action(context(), action("code_search", args))
        finally:
            await gateway.close()
        assert result["success"] is False and result["error"]["code"] == code and seen == []
        assert [call[0] for call in fake.calls] == ["meta"]

    # 管理员关掉开关后，已下载的源码也不能再用
    fake = FakeCode(META)
    gateway, seen = gateway_for([(403, {"success": False, "code": "ai_download_disabled", "message": DISABLED})], fake)
    try:
        denied = await gateway.execute_action(context(), action("code_search", args))
    finally:
        await gateway.close()
    assert denied["error"]["code"] == "ai_download_disabled" and [call[0] for call in fake.calls] == ["meta"]

    fake = FakeCode(META)
    gateway, seen = gateway_for([(200, {"success": True, "data": {"ai_download": True}})], fake)
    try:
        result = await gateway.execute_action(context(), action("code_search", args))
    finally:
        await gateway.close()
    [request] = seen
    assert request.url.path == "/api/ai/internal/code-access" and dict(request.url.params) == {"env": ENV}
    assert fake.calls[-1] == ("search", CODE_ID, {"pattern": "throw new", "glob": "**/*.java", "fixed_strings": True,
                                                  "context": 2, "max_results": 20})
    assert result["success"] is True and result["freshness"] == "local" and result["truncated"] is False
    assert result["data"]["matches"][0]["path"] == "src/A.java"


@pytest.mark.asyncio
async def test_code_read_passes_line_range_and_truncation():
    fake = FakeCode(META)
    gateway, _ = gateway_for([(200, {"success": True, "data": {"ai_download": True}})], fake)
    try:
        result = await gateway.execute_action(context(), action("code_read", {
            "code_id": CODE_ID, "path": "src/A.java", "start_line": 40, "lines": 80}))
    finally:
        await gateway.close()
    assert fake.calls[-1] == ("read", CODE_ID, "src/A.java", {"start_line": 40, "lines": 80})
    assert result["success"] is True and result["truncated"] is True and result["data"]["total_lines"] == 9


@pytest.mark.asyncio
async def test_code_symbols_passes_options_after_access_check():
    fake = FakeCode(META)
    gateway, seen = gateway_for([(200, {"success": True, "data": {"ai_download": True}})], fake)
    try:
        result = await gateway.execute_action(context(), action("code_symbols", {
            "code_id": CODE_ID, "name": "A.create", "kind": "method", "max_results": 10, "namespace": "ops"}))
    finally:
        await gateway.close()
    assert [request.url.path for request in seen] == ["/api/ai/internal/code-access"]
    assert fake.calls[-1] == ("symbols", CODE_ID, {"name": "A.create", "kind": "method", "max_results": 10})
    assert result["success"] is True and result["freshness"] == "local"
    assert result["data"]["symbols"][0]["line"] == 3


@pytest.mark.asyncio
async def test_code_target_defaults_to_selected_pod_then_deployment():
    selected = {"env": ENV, "namespace": "ops", "deployment": {"namespace": "ops", "name": "app"},
                "pod": {"namespace": "ops", "name": POD}}
    cases = [
        (selected, {}, {"namespace": "ops", "pod": POD}),
        ({**selected, "pod": None}, {}, {"namespace": "ops", "deployment": "app"}),
        # 模型指定了服务就用指定的
        (selected, {"deployment": "other"}, {"namespace": "ops", "deployment": "other"}),
    ]
    for scope, arguments, expected in cases:
        gateway, _ = gateway_for([], FakeCode())
        gateway.execute_action = AsyncMock(return_value={"success": True})
        try:
            await gateway.invoke(context(scope=scope), "code_info", arguments, call_id=f"call-{len(arguments)}-{scope['pod'] is None}", mcp=True)
        finally:
            await gateway.close()
        assert gateway.execute_action.await_args.args[1]["arguments"] == expected


@pytest.mark.asyncio
async def test_code_operations_are_read_only_but_need_cluster_access():
    gateway, _ = gateway_for([], FakeCode(META))
    gateway.execute_action = AsyncMock(return_value={"success": True})
    try:
        for operation, arguments in (("code_info", {"namespace": "ops", "deployment": "app"}),
                                     ("code_fetch", {"namespace": "ops", "pod": POD}),
                                     ("code_search", {"code_id": CODE_ID, "pattern": "x"}),
                                     ("code_read", {"code_id": CODE_ID, "path": "a.py"}),
                                     ("code_symbols", {"code_id": CODE_ID, "name": "create"})):
            assert gateway.read_only(operation, arguments) is True
            await gateway.invoke(context("read"), operation, arguments, call_id=f"ok-{operation}", mcp=True)
            with pytest.raises(AIError) as denied:
                await gateway.invoke(context("none"), operation, arguments, call_id=f"denied-{operation}", mcp=True)
            assert denied.value.status == 403
        assert gateway.execute_action.await_count == 5
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_cleanup_loop_survives_errors(monkeypatch):
    import kubedoor_ai.gateway as gateway_module

    calls = []

    class Flaky(FakeCode):
        async def cleanup(self):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk")
            raise SystemExit  # 第二次结束循环

    monkeypatch.setattr(gateway_module, "CODE_CLEANUP_INTERVAL", 0)
    gateway, _ = gateway_for([], Flaky())
    try:
        with pytest.raises(SystemExit):
            await gateway.code_cleanup_loop()
    finally:
        await gateway.close()
    assert len(calls) == 2  # 第一次出错只记日志，循环继续
