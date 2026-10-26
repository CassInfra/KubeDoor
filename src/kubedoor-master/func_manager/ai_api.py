"""Authenticated, cluster-bound bridges used by the AI service.

The browser never reaches these endpoints: nginx rejects /api/ai/internal/.
Tool execution travels over the existing authenticated agent WebSocket, rather
than exposing an additional privileged HTTP listener in each managed cluster.

The auth middleware verifies the internal token and loads the acting user named
by X-User-Name from the database. Cluster permissions are always evaluated here
from that user record; the X-User-Permission header sent by the AI service is
ignored.
"""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp
from aiohttp import web

from auth import audit
from auth.context import client_ip
from func_manager.deployment_control import deployment_control as control_status, valid_target


def authorize(request):
    """Return the acting user resolved by the auth middleware (internal token + X-User-Name)."""
    principal = request.get("principal")
    if principal is None or principal.via != "internal" or not principal.is_human:
        raise web.HTTPForbidden(text="缺少可信用户身份")
    return principal


def require_cluster(principal, env):
    if not principal.can_read(env):
        raise web.HTTPForbidden(text=f"没有集群 {env} 的访问权限")


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
        principal = authorize(request)
        # 集群别名在 Web 的 Agent 管理页维护,AI 助手按别名识别用户说的是哪个集群;库里还没有的集群给空列表
        aliases = await utils.get_k8s_aliases()
        envs = sorted(set(aliases) | set(clients))
        return web.json_response({
            "enabled": True,
            "username": principal.username, "permission": principal.role, "role": principal.role,
            "default_permission": principal.default_permission,
            "cluster_permissions": dict(principal.clusters),
            # "none" clusters are invisible to this user everywhere, including the AI assistant.
            "clusters": [{"env": env, "online": bool(clients.get(env, {}).get("online")),
                          "ai_tools": bool(clients.get(env, {}).get("ai_tools")),
                          "ver": clients.get(env, {}).get("ver", ""),
                          "permission": principal.permission(env),
                          "aliases": aliases.get(env, [])}
                         for env in envs if principal.can_read(env)],
            "prom_type": utils.PROM_TYPE, "prom_label_key": utils.PROM_K8S_TAG_KEY,
        })

    async def identity(request):
        # The trusted internal-auth middleware has already resolved X-User-Name
        # to a live, enabled human principal. Never accept role/cluster claims
        # from the AI service itself.
        principal = authorize(request)
        return web.json_response({
            "username": principal.username,
            "permission": principal.role,
            "clusters": dict(principal.clusters),
            "default_permission": principal.default_permission,
        })

    async def execute(request):
        principal = authorize(request)
        env = request.query.get("env", "")
        require_cluster(principal, env)
        body = await request.json()
        if not isinstance(body, dict) or body.get("phase", "execute") not in ("prepare", "execute"):
            raise web.HTTPBadRequest(text="无效执行阶段")
        if body.get("operation") not in ("api", "kubectl", "istioctl", "diagnostic", "pod_exec"):
            raise web.HTTPBadRequest(text="未知工具类型")
        if not isinstance(body.get("arguments", {}), dict):
            raise web.HTTPBadRequest(text="arguments 必须为对象")
        if clients.get(env, {}).get("online") and not clients[env].get("ai_tools"):
            raise web.HTTPNotImplemented(text="此 Agent 版本不支持通用 AI 工具，请升级 Agent 或上传 kubeconfig；现有数据接口仍可查询")
        writable = principal.can_write(env)
        if not writable and body.get("approved"):
            raise web.HTTPForbidden(text=f"当前账号对集群 {env} 只有只读权限，不能批准集群修改")
        timeout = min(max(float(body.get("timeout", 120)), 1), 300)
        approved_write = body.get("approved") is True and body.get("phase", "execute") == "execute"
        result = await agent_rpc(clients, env, {
            "phase": body.get("phase", "execute"), "operation": body["operation"],
            "arguments": body.get("arguments", {}), "call_id": body.get("call_id", ""),
            "approved": body.get("approved") is True, "timeout": timeout,
            "username": principal.username, "permission": "rw" if writable else "read",
        }, timeout=timeout + 10)
        if approved_write:
            audit.record_bg(principal.username, "ai.agent_execute", env=env, target=str(body["operation"]),
                            ip=client_ip(request), success=bool(isinstance(result, dict) and result.get("success", True)),
                            detail={"call_id": str(body.get("call_id", ""))[:160]})
        return web.json_response(result)

    async def cancel(request):
        principal = authorize(request)
        env = request.query.get("env", "")
        require_cluster(principal, env)
        body = await request.json()
        result = await agent_rpc(clients, env, {
            "phase": "cancel", "call_id": body.get("call_id", ""),
            "username": principal.username, "permission": "rw" if principal.can_write(env) else "read",
        }, timeout=10)
        return web.json_response(result)

    async def metrics(request):
        principal = authorize(request)
        body = await request.json()
        envs = set(await utils.get_k8s_names()) | set(clients)
        if body.get("env") not in envs:
            raise web.HTTPBadRequest(text="未知 K8S 集群")
        require_cluster(principal, body["env"])
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

    async def deployment_control(request):
        # AI 改副本数、资源、JVM 前先问这里:受花折资源管控时只能经管控表生效,直接改会被准入控制改回
        principal = authorize(request)
        env = request.query.get("env", "")
        namespace, deployment = request.query.get("namespace", ""), request.query.get("deployment", "")
        require_cluster(principal, env)
        if not valid_target(namespace, deployment):
            raise web.HTTPBadRequest(text="namespace 和 deployment 必须是合法的 K8S 名称")
        data = await control_status(clients, agent_rpc, utils.pg_fetchrow, env, namespace, deployment, principal.username)
        return web.json_response({"success": True, "data": data})

    app.router.add_get("/api/ai/internal/bootstrap", bootstrap)
    app.router.add_get("/api/ai/internal/identity", identity)
    app.router.add_post("/api/ai/internal/agent-execute", execute)
    app.router.add_post("/api/ai/internal/agent-cancel", cancel)
    app.router.add_post("/api/ai/internal/metrics", metrics)
    app.router.add_get("/api/ai/internal/deployment-control", deployment_control)
