"""CoreDNS 管理:kube-system/coredns 这个 ConfigMap 的读取和保存

Corefile 的解析(hosts 块拆成表格、改写某一行)都在前端做,agent 只负责:
- 读取:返回 ConfigMap 原始对象(去掉 managedFields);
- 保存:JSON merge patch,带上页面加载时的 metadata.resourceVersion 做乐观锁 ——
  加载之后别人改过(别的页面、kubectl、Helm)就返回冲突,不会把别人的修改覆盖掉。
检查是否生效用的是通用的网络测试接口(func_manager/net_probe.py):从 agent 所在 Pod 解析域名,和业务 Pod 看到的一样。
"""

import json

from aiohttp import web
from kubernetes_asyncio.client import rest
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger

NAMESPACE = "kube-system"
NAME = "coredns"
COREFILE_KEY = "Corefile"
API_TIMEOUT = 15
_MAP_FIELDS = (("data",), ("binaryData",), ("metadata", "labels"), ("metadata", "annotations"))


def _fail(message, status=400, **extra):
    return web.json_response({"success": False, "error": message, **extra}, status=status)


def _api_error(exc, action):
    if exc.status == 404:
        return _fail(f"{NAMESPACE} 命名空间下没有 ConfigMap {NAME}(集群可能没有使用 CoreDNS)", 404)
    logger.error(f"[coredns] {action}失败: {exc.status} {exc.reason} {getattr(exc, 'body', '')}")
    return _fail(f"{action} CoreDNS 配置失败: {_api_message(exc)}", exc.status or 500)


def _api_message(exc):
    try:
        message = json.loads(exc.body).get("message")
    except Exception:
        message = None
    return message or exc.reason or str(exc)


async def _call_raw(api_fn, **kwargs):
    """调用 API 拿原始 JSON(不反序列化成模型对象,ConfigMap 原样往返);非 2xx 抛 ApiException"""
    kwargs.setdefault("_request_timeout", API_TIMEOUT)
    resp = await api_fn(**kwargs, _preload_content=False)
    try:
        body = await resp.read()
    finally:
        resp.release()
    if not 200 <= resp.status <= 299:
        exc = ApiException(http_resp=rest.RESTResponse(resp, body))
        exc.body = body.decode("utf-8", "replace")
        raise exc
    return json.loads(body)


def _clean(configmap):
    (configmap.get("metadata") or {}).pop("managedFields", None)
    return configmap


# ---------------------------------------------------------------------------------------------- Corefile 词法检查
def _tokens(text):
    """与 CoreDNS(caddy v1)的 Caddyfile 词法一致:引号外的 # 到行尾是注释,"..." 是一个记号(可以跨行)
    返回 [(记号, 行号)];引号没闭合抛 ValueError"""
    tokens, value, line, start = [], [], 1, 1
    in_token = quoted = escaped = comment = False
    for ch in text:
        if quoted:
            if not escaped:
                if ch == "\\":
                    escaped = True
                    continue
                if ch == '"':
                    quoted = in_token = False
                    tokens.append(("".join(value), start))
                    value = []
                    continue
            if ch == "\n":
                line += 1
            if escaped and ch != '"':
                value.append("\\")
            value.append(ch)
            escaped = False
            continue
        if ch.isspace():
            if ch == "\n":
                line += 1
                comment = False
            if in_token:
                tokens.append(("".join(value), start))
                value, in_token = [], False
            continue
        if ch == "#":
            comment = True
        if comment:
            continue
        if not in_token:
            in_token, start = True, line
            if ch == '"':
                quoted = True
                continue
        value.append(ch)
    if quoted:
        raise ValueError(f"第 {start} 行的引号没有闭合")
    if in_token:
        tokens.append(("".join(value), start))
    return tokens


def check_corefile(text):
    """保存前的最后一道检查:大括号配对、引号闭合。返回错误信息,没问题返回 None

    只能挡住明显写坏的配置(详细校验在前端)。CoreDNS 的 reload 遇到解析失败会继续用旧配置,
    但 Pod 一重启就起不来,整个集群的 DNS 都会挂。
    """
    if not isinstance(text, str) or not text.strip():
        return "Corefile 不能为空"
    try:
        tokens = _tokens(text)
    except ValueError as exc:
        return f"Corefile 格式错误: {exc}"
    opened = []
    for token, line in tokens:
        if token == "{":
            opened.append(line)
        elif token == "}":
            if not opened:
                return f"Corefile 格式错误: 第 {line} 行多了一个 }}"
            opened.pop()
    if opened:
        return f"Corefile 格式错误: 第 {opened[-1]} 行的 {{ 没有闭合"
    return None


# ---------------------------------------------------------------------------------------------- 读取 / 保存
async def get_coredns(core_v1, request):
    try:
        configmap = await _call_raw(core_v1.read_namespaced_config_map, name=NAME, namespace=NAMESPACE)
    except ApiException as exc:
        return _api_error(exc, "读取")
    return web.json_response({"success": True, "data": _clean(configmap)})


def _validate_patch(resource_version, patch):
    if not isinstance(resource_version, str) or not resource_version.strip():
        return "缺少 resource_version(页面加载时的版本号)"
    if not isinstance(patch, dict) or not patch:
        return "没有需要保存的修改"
    if set(patch) - {"data", "binaryData", "metadata"}:
        return "只能修改 data、binaryData、metadata.labels、metadata.annotations"
    metadata = patch.get("metadata", {})
    if not isinstance(metadata, dict) or set(metadata) - {"labels", "annotations"}:
        return "只能修改 data、binaryData、metadata.labels、metadata.annotations"
    for path in _MAP_FIELDS:
        value = patch
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            continue
        if not isinstance(value, dict):
            return f"{'.'.join(path)} 必须是对象"
        for key, item in value.items():
            if not isinstance(key, str) or not key or (item is not None and not isinstance(item, str)):
                return f"{'.'.join(path)}.{key} 的值必须是字符串(删除用 null)"
    data = patch.get("data") or {}
    if COREFILE_KEY in data:
        if data[COREFILE_KEY] is None:
            return "不能删除 Corefile"
        return check_corefile(data[COREFILE_KEY])
    return None


async def save_coredns(core_v1, request):
    """POST {resource_version, patch: {data?, binaryData?, metadata?: {labels?, annotations?}}}

    patch 按 JSON merge patch 语义(值为 null 表示删除这个键),附带 resourceVersion 前置条件。
    """
    try:
        body = await request.json()
    except Exception:
        return _fail("请求体不是有效的 JSON")
    if not isinstance(body, dict):
        return _fail("请求体格式错误")
    resource_version, patch = body.get("resource_version"), body.get("patch")
    error = _validate_patch(resource_version, patch)
    if error:
        return _fail(error)
    merge = {key: value for key, value in patch.items() if key != "metadata"}
    merge["metadata"] = {**(patch.get("metadata") or {}), "resourceVersion": resource_version.strip()}
    summary = request.query.get("summary") or ""
    try:
        configmap = await _call_raw(
            core_v1.patch_namespaced_config_map, name=NAME, namespace=NAMESPACE, body=merge,
            _content_type="application/merge-patch+json")
    except ApiException as exc:
        if exc.status == 409:
            return _fail("CoreDNS 配置在你加载之后被修改过(别人或别的页面保存过),请刷新后再改", 409, conflict=True)
        return _api_error(exc, "保存")
    logger.info(f"[coredns] 已保存 {NAMESPACE}/{NAME} "
                f"({resource_version} -> {(configmap.get('metadata') or {}).get('resourceVersion')}): {summary}")
    return web.json_response({"success": True, "data": _clean(configmap)})
