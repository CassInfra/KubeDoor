"""coredns_manager:读取 / 乐观锁保存 kube-system/coredns。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

COREFILE = """.:53 {
    hosts {
      10.114.1.4    a.example.com    # 备注
      fallthrough
    }
    reload
}
"""


class FakeResp:
    def __init__(self, data, status=200):
        self.status = status
        self.reason = "OK" if status < 400 else "Error"
        self._body = json.dumps(data).encode() if not isinstance(data, bytes) else data
        self.released = False

    async def read(self):
        return self._body

    def release(self):
        self.released = True

    # rest.RESTResponse 需要的属性
    @property
    def headers(self):
        return {}


def raw_api(*responses):
    return AsyncMock(side_effect=list(responses))


class FakeRequest:
    def __init__(self, query=None, body=None):
        self.query = query or {}
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def body(resp):
    return json.loads(resp.body)


def configmap(rv="100", corefile=COREFILE):
    return {"kind": "ConfigMap", "apiVersion": "v1",
            "metadata": {"name": "coredns", "namespace": "kube-system", "resourceVersion": rv,
                         "managedFields": [{"manager": "kubectl"}], "labels": {"a": "b"}},
            "data": {"Corefile": corefile}}


@pytest.fixture
def mod(coredns_module):
    return coredns_module


# ---------------------------------------------------------------------------------------------- 读取
@pytest.mark.asyncio
async def test_get_returns_raw_object_without_managed_fields(mod):
    core = SimpleNamespace(read_namespaced_config_map=raw_api(FakeResp(configmap())))
    resp = await mod.get_coredns(core, FakeRequest())
    data = body(resp)["data"]
    assert resp.status == 200 and data["data"]["Corefile"] == COREFILE
    assert "managedFields" not in data["metadata"] and data["metadata"]["resourceVersion"] == "100"
    kwargs = core.read_namespaced_config_map.await_args.kwargs
    assert kwargs["name"] == "coredns" and kwargs["namespace"] == "kube-system" and kwargs["_preload_content"] is False


@pytest.mark.asyncio
async def test_get_not_found(mod):
    core = SimpleNamespace(read_namespaced_config_map=raw_api(FakeResp({"message": "not found"}, 404)))
    resp = await mod.get_coredns(core, FakeRequest())
    assert resp.status == 404 and "没有 ConfigMap coredns" in body(resp)["error"]


# ---------------------------------------------------------------------------------------------- 保存
@pytest.mark.parametrize("corefile,error", [
    (COREFILE, None),
    (".:53 {\n  hosts {\n    10.0.0.1 a.com\n  }\n", "第 1 行的 { 没有闭合"),
    (".:53 {\n}\n}\n", "第 3 行多了一个 }"),
    ('.:53 {\n  log "abc\n}\n', "引号没有闭合"),
    # 注释里的大括号不算;引号里的 "{" 和 CoreDNS(caddy v1)一样也当大括号
    ('.:53 { # }\n  rewrite name "{" b\n  hosts {\n    10.0.0.1 a.com # {{\n  }\n}\n', "第 1 行的 { 没有闭合"),
    (".:53 { # }\n  hosts {\n    10.0.0.1 a.com # {{\n  }\n}\n", None),
    ("  \n", "Corefile 不能为空"),
])
def test_check_corefile(mod, corefile, error):
    result = mod.check_corefile(corefile)
    assert result == error if error is None else error in result


@pytest.mark.asyncio
async def test_save_merge_patch_with_resource_version(mod):
    core = SimpleNamespace(patch_namespaced_config_map=raw_api(FakeResp(configmap("101"))))
    patch = {"data": {"Corefile": COREFILE}, "metadata": {"labels": {"x": None}}}
    resp = await mod.save_coredns(core, FakeRequest({"summary": "修改记录"}, {"resource_version": "100", "patch": patch}))
    assert resp.status == 200 and body(resp)["data"]["metadata"]["resourceVersion"] == "101"
    kwargs = core.patch_namespaced_config_map.await_args.kwargs
    assert kwargs["body"] == {"data": {"Corefile": COREFILE},
                              "metadata": {"labels": {"x": None}, "resourceVersion": "100"}}
    assert kwargs["_content_type"] == "application/merge-patch+json"
    assert kwargs["name"] == "coredns" and kwargs["namespace"] == "kube-system"


@pytest.mark.asyncio
async def test_save_conflict(mod):
    core = SimpleNamespace(patch_namespaced_config_map=raw_api(FakeResp({"message": "modified"}, 409)))
    resp = await mod.save_coredns(core, FakeRequest(body={"resource_version": "99", "patch": {"data": {"x": "y"}}}))
    data = body(resp)
    assert resp.status == 409 and data["conflict"] is True and "刷新" in data["error"]


@pytest.mark.asyncio
async def test_save_api_error_uses_status_message(mod):
    core = SimpleNamespace(patch_namespaced_config_map=raw_api(FakeResp({"message": "field is immutable"}, 422)))
    resp = await mod.save_coredns(core, FakeRequest(body={"resource_version": "1", "patch": {"data": {"x": "y"}}}))
    assert resp.status == 422 and "field is immutable" in body(resp)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,error", [
    ({"patch": {"data": {"x": "y"}}}, "resource_version"),
    ({"resource_version": "1", "patch": {}}, "没有需要保存的修改"),
    ({"resource_version": "1", "patch": {"spec": {}}}, "只能修改"),
    ({"resource_version": "1", "patch": {"metadata": {"name": "x"}}}, "只能修改"),
    ({"resource_version": "1", "patch": {"data": {"x": 1}}}, "data.x 的值必须是字符串"),
    ({"resource_version": "1", "patch": {"metadata": {"labels": []}}}, "metadata.labels 必须是对象"),
    ({"resource_version": "1", "patch": {"data": {"Corefile": None}}}, "不能删除 Corefile"),
    ({"resource_version": "1", "patch": {"data": {"Corefile": ".:53 {\n"}}}, "没有闭合"),
    ("not a dict", "请求体格式错误"),
])
async def test_save_validation(mod, payload, error):
    core = SimpleNamespace(patch_namespaced_config_map=AsyncMock())
    resp = await mod.save_coredns(core, FakeRequest(body=payload))
    assert resp.status == 400 and error in body(resp)["error"]
    core.patch_namespaced_config_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_rejects_invalid_json(mod):
    resp = await mod.save_coredns(SimpleNamespace(), FakeRequest(body=ValueError("bad")))
    assert resp.status == 400 and "JSON" in body(resp)["error"]
