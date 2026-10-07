"""Authenticated, cluster-bound bridges used by the AI service.

The browser never reaches these endpoints: nginx rejects /api/ai/internal/.
Tool execution travels over the existing authenticated agent WebSocket, rather
than exposing an additional privileged HTTP listener in each managed cluster.
"""

import asyncio
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp
from aiohttp import web


def authorize(request):
    expected = os.environ.get("AI_INTERNAL_TOKEN", "")
    supplied = request.headers.get("X-Kubedoor-Token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise web.HTTPUnauthorized(text="AI 内部接口认证失败")
    username = request.headers.get("X-User-Name", "")
    permission = request.headers.get("X-User-Permission", "")
    if not username or permission not in ("read", "rw"):
        raise web.HTTPForbidden(text="缺少可信用户身份")
    return username, permission


def _timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("时间必须携带时区")
        return parsed.timestamp()


def _step_seconds(value):
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", str(value))
    if not match:
        raise ValueError("step 必须为秒数或 15s、5m 等时长")
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def metrics_parameters(body, label_key, max_days=30):
    env, query = body.get("env"), body.get("query")
    if not isinstance(env, str) or not env or not isinstance(query, str) or not query.strip():
        raise ValueError("env 和 query 不能为空")
    if len(query) > 16384 or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", label_key):
        raise ValueError("查询过长或集群标签配置无效")
    # VM applies this filter to every series selector, including selectors
    # nested inside aggregations. Caller-supplied extra_label is never accepted.
    params = {"query": query, "extra_label": f"{label_key}={env}"}
    ranged = body.get("start") is not None or body.get("end") is not None
    if ranged:
        end = _timestamp(body.get("end", time.time()))
        start = _timestamp(body.get("start", end - 3600))
        if not 0 < end - start <= max_days * 86400:
            raise ValueError(f"时间区间必须大于 0 且不超过 {max_days} 天")
        step = _step_seconds(body.get("step", max(15, (end - start) / 11000)))
        if step <= 0 or (end - start) / step > 11000:
            raise ValueError("单个时间序列超过 11000 点，请增大 step")
        params.update(start=start, end=end, step=step)
    elif body.get("time") is not None:
        params["time"] = _timestamp(body["time"])
    return "query_range" if ranged else "query", params


async def agent_rpc(clients, env, payload, timeout=130):
    connection = clients.get(env)
    if not connection or not connection.get("online"):
        raise web.HTTPServiceUnavailable(text="Agent 不在线，请使用已配置的 kubeconfig")
    request_id = uuid.uuid4().hex
    events = connection.setdefault("response_events", {})
    queue = connection.setdefault("response_queue", {})
    ai_ids = connection.setdefault("ai_request_ids", set())
    event = asyncio.Event()
    events[request_id] = event
    ai_ids.add(request_id)
    try:
        async with asyncio.timeout(timeout):
            await connection["ws"].send_json({"type": "ai_tool", "request_id": request_id, **payload})
            await event.wait()
        return queue.pop(request_id)
    except asyncio.TimeoutError:
        # Do not resend: a timed-out Kubernetes write may have completed.
        raise web.HTTPGatewayTimeout(text="Agent 执行超时，操作结果可能未知，请先查询实际状态")
    finally:
        events.pop(request_id, None)
        queue.pop(request_id, None)
        ai_ids.discard(request_id)


def register_routes(app, clients, utils):
    async def bootstrap(request):
        username, permission = authorize(request)
        envs = sorted(set(await utils.get_k8s_names()) | set(clients))
        return web.json_response({
            "enabled": True,
            "username": username, "permission": permission,
            "clusters": [{"env": env, "online": bool(clients.get(env, {}).get("online")),
                          "ai_tools": bool(clients.get(env, {}).get("ai_tools")),
                          "ver": clients.get(env, {}).get("ver", "")} for env in envs],
            "prom_type": utils.PROM_TYPE, "prom_label_key": utils.PROM_K8S_TAG_KEY,
        })

    async def execute(request):
        username, permission = authorize(request)
        env = request.query.get("env", "")
        body = await request.json()
        if not isinstance(body, dict) or body.get("phase", "execute") not in ("prepare", "execute"):
            raise web.HTTPBadRequest(text="无效执行阶段")
        if body.get("operation") not in ("api", "kubectl", "istioctl", "diagnostic", "pod_exec"):
            raise web.HTTPBadRequest(text="未知工具类型")
        if not isinstance(body.get("arguments", {}), dict):
            raise web.HTTPBadRequest(text="arguments 必须为对象")
        if clients.get(env, {}).get("online") and not clients[env].get("ai_tools"):
            raise web.HTTPNotImplemented(text="此 Agent 版本不支持通用 AI 工具，请升级 Agent 或上传 kubeconfig；现有数据接口仍可查询")
        if permission != "rw" and body.get("approved"):
            raise web.HTTPForbidden(text="只读用户不能批准集群修改")
        timeout = min(max(float(body.get("timeout", 120)), 1), 300)
        result = await agent_rpc(clients, env, {
            "phase": body.get("phase", "execute"), "operation": body["operation"],
            "arguments": body.get("arguments", {}), "call_id": body.get("call_id", ""),
            "approved": body.get("approved") is True, "timeout": timeout,
            "username": username, "permission": permission,
        }, timeout=timeout + 10)
        return web.json_response(result)

    async def cancel(request):
        username, permission = authorize(request)
        body = await request.json()
        result = await agent_rpc(clients, request.query.get("env", ""), {
            "phase": "cancel", "call_id": body.get("call_id", ""),
            "username": username, "permission": permission,
        }, timeout=10)
        return web.json_response(result)

    async def metrics(request):
        authorize(request)
        body = await request.json()
        envs = set(await utils.get_k8s_names()) | set(clients)
        if body.get("env") not in envs:
            raise web.HTTPBadRequest(text="未知 K8S 集群")
        if "victoria" not in (utils.PROM_TYPE or "").lower():
            raise web.HTTPNotImplemented(text="自由指标查询需要 VictoriaMetrics；现有 Prometheus 指标接口仍可使用")
        if not utils.PROM_URL:
            raise web.HTTPServiceUnavailable(text="未配置 VictoriaMetrics 查询地址")
        try:
            suffix, params = metrics_parameters(body, utils.PROM_K8S_TAG_KEY, int(os.environ.get("AI_METRICS_MAX_RANGE_DAYS", "30")))
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        upstream = urlsplit(utils.PROM_URL)
        auth = aiohttp.BasicAuth(unquote(upstream.username), unquote(upstream.password or "")) if upstream.username else None
        host = upstream.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if upstream.port:
            host += f":{upstream.port}"
        base = urlunsplit((upstream.scheme, host, upstream.path.rstrip("/"), "", ""))
        limit = int(os.environ.get("AI_METRICS_MAX_BYTES", "2097152"))
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(f"{base}/api/v1/{suffix}", params=params, auth=auth, allow_redirects=False) as response:
                    if response.status != 200:
                        raise web.HTTPBadGateway(text=f"指标服务返回 HTTP {response.status}")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > limit:
                            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(raw))
                    data = json.loads(raw)
                    if data.get("status") != "success":
                        raise web.HTTPBadGateway(text="指标查询失败，请检查 MetricsQL 表达式")
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            raise web.HTTPBadGateway(text="指标服务无法访问或返回格式错误")
        return web.json_response({"success": True, "data": data["data"], "source": "victoriametrics",
                                  "observed_at": datetime.now(timezone.utc).isoformat(),
                                  "query": params["query"], "cluster": body["env"],
                                  "start": params.get("start"), "end": params.get("end"), "truncated": False})

    app.router.add_get("/api/ai/internal/bootstrap", bootstrap)
    app.router.add_post("/api/ai/internal/agent-execute", execute)
    app.router.add_post("/api/ai/internal/agent-cancel", cancel)
    app.router.add_post("/api/ai/internal/metrics", metrics)
