from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, AsyncExitStack

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .domain import AIError, Provider, authenticate, normalize_scope, redact
from .runtime import SKILLS, SKILLS_ROOT
from .service import Service
from .storage import plain


class Authentication:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        try:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            scope["identity"] = authenticate(headers, self.token)
        except AIError as exc:
            await JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def body(request):
    try:
        value = await request.json()
    except Exception:
        raise AIError("请求必须为有效 JSON") from None
    if not isinstance(value, dict):
        raise AIError("请求必须为 JSON 对象")
    return value


def create_app(service=None, token=None):
    from .mcp import create_mcp
    token = token if token is not None else os.getenv("AI_INTERNAL_TOKEN", "")
    mcp = create_mcp(lambda: app.state.service, token)
    streamable = mcp.http_app(path="/mcp", transport="http")
    legacy = mcp.http_app(path="/sse", transport="sse")

    @asynccontextmanager
    async def lifespan(app):
        owned = service is None
        app.state.service = service or await Service.open(os.getenv("MASTER_URL", "http://kubedoor-master"), token, os.getenv("AI_ENCRYPTION_KEY", ""))
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(streamable.router.lifespan_context(streamable))
                await stack.enter_async_context(legacy.router.lifespan_context(legacy))
                yield
        finally:
            if owned:
                await app.state.service.close()

    async def handle(request: Request):
        svc, identity = request.app.state.service, request.scope["identity"]
        endpoint = request.path_params.get("endpoint", "")
        sid, rid, env = request.path_params.get("sid"), request.path_params.get("rid"), request.path_params.get("env")
        mid = request.path_params.get("mid")
        try:
            if endpoint == "bootstrap":
                result = await svc.bootstrap(identity)
            elif endpoint == "resources":
                result = await svc.resources(identity, request.query_params)
            elif endpoint == "connections-summary":
                result = {"connections": await svc.store.summaries()}
            elif endpoint == "connections":
                result = {"connections": await svc.store.summaries()}
            elif endpoint == "parse":
                identity.require_write()
                data = await body(request)
                parsed = await svc.parse_connection(data.get("content"), data.get("context"))
                result = {"contexts": parsed.get("contexts", []), "current_context": parsed.get("context"), "requires_context": parsed.get("requires_context", False)}
            elif endpoint == "connection-test":
                data = await body(request)
                tested, _, _ = await svc.test_connection(identity, env, data)
                return JSONResponse(redact(tested), status_code=200 if tested.get("success", True) else 422)
            elif endpoint == "connection":
                identity.require_write()
                if request.method == "DELETE":
                    await svc.store.execute("DELETE FROM kubedoor_ai_connections WHERE env=$1", env)
                    result = {"success": True}
                else:
                    data = await body(request)
                    if not data.get("content") or not data.get("context"):
                        raise AIError("保存连接需要 content 和明确 context")
                    tested, config, context = await svc.test_connection(identity, env, data)
                    if tested.get("success") is False:
                        return JSONResponse(redact(tested), status_code=422)
                    encrypted = svc.gateway.cipher.encrypt(env, config)
                    result = await svc.store.one("INSERT INTO kubedoor_ai_connections(env,encrypted_config,context,updated_by) VALUES($1,$2,$3,$4) ON CONFLICT(env) DO UPDATE SET encrypted_config=EXCLUDED.encrypted_config,context=EXCLUDED.context,revision=kubedoor_ai_connections.revision+1,test_status='success',tested_at=now(),updated_by=EXCLUDED.updated_by,updated_at=now() RETURNING env,context,revision,test_status,tested_at", env, encrypted, context, identity.username)
            elif endpoint == "sessions":
                if request.method == "GET":
                    result = {"sessions": await svc.store.fetch("SELECT id,title,created_at,updated_at FROM kubedoor_ai_sessions WHERE username=$1 ORDER BY updated_at DESC", identity.username)}
                else:
                    data = await body(request)
                    result = await svc.new_session(identity, data.get("title", "新会话"), request.headers.get("idempotency-key"))
            elif endpoint == "memories":
                if request.method == "GET":
                    result = await svc.memory_library.list(request.query_params)
                else:
                    result = await svc.memory_library.create(identity, await body(request))
            elif endpoint == "memory":
                if request.method == "GET":
                    result = await svc.memory_library.get(mid)
                elif request.method == "PATCH":
                    result = await svc.memory_library.update(identity, mid, await body(request))
                else:
                    result = await svc.memory_library.delete(identity, mid, request.query_params.get("version"))
            elif endpoint == "memory-summary":
                result = await svc.memory_library.summarize(identity, sid, (await body(request)).get("provider"))
            elif endpoint == "title":
                data = await body(request)
                result = await svc.generate_session_title(
                    identity, sid, data.get("message"), data.get("provider")
                )
            elif endpoint == "session":
                if request.method == "GET":
                    result = await svc.session_detail(identity, sid)
                elif request.method == "PATCH":
                    await svc.store.session(sid, identity)
                    title = (await body(request)).get("title")
                    if not isinstance(title, str) or not title.strip():
                        raise AIError("会话标题不能为空")
                    result = await svc.store.one("UPDATE kubedoor_ai_sessions SET title=$2,updated_at=now() WHERE id=$1::uuid RETURNING id,title,updated_at", sid, title.strip()[:200])
                else:
                    active = (await svc.session_detail(identity, sid))["active_run"]
                    if active:
                        await svc.runtime.cancel(await svc.store.run(active["id"], identity), identity)
                    await svc.store.execute("DELETE FROM kubedoor_ai_sessions WHERE id=$1::uuid", sid)
                    if hasattr(svc.runtime.checkpointer, "adelete_thread"):
                        await svc.runtime.checkpointer.adelete_thread(sid)
                    result = {"success": True}
            elif endpoint == "runs":
                data = await body(request)
                message = data.get("message")
                if not isinstance(message, str) or not message.strip() or len(message) > 100000:
                    raise AIError("消息不能为空，最多 100000 字符")
                provider = Provider.parse(data.get("provider"))
                run, created = await svc.new_run(identity, sid, data.get("scope"), skill_ids=data.get("skill_ids"), key=request.headers.get("idempotency-key"), memory_ids=data.get("memory_ids"))
                saved_message = None
                if created:
                    clean_message = redact(message.strip(), (provider.api_key,))
                    memories = redact(run.get("_memories", []), (provider.api_key,))
                    if memories:
                        saved_message = await svc.store.message(run, "user", clean_message, memories=memories)
                        run["_memories"] = saved_message.get("memories", memories)
                    else:
                        await svc.store.message(run, "user", clean_message)
                    svc.runtime.start(run, identity, provider, clean_message)
                elif data.get("memory_ids"):
                    # Retrying an accepted request must return the original
                    # attachment, even if the shared memory was edited/deleted.
                    saved_message = await svc.store.one("SELECT id,role,content,scope,tools,memories,created_at FROM kubedoor_ai_messages WHERE run_id=$1::uuid AND role='user' ORDER BY created_at,id LIMIT 1", run["id"])
                result = {"run_id": run["id"], "status": run["status"]}
                if saved_message:
                    result["message"] = saved_message
            elif endpoint == "decisions":
                result = await svc.decision(identity, rid, await body(request))
            elif endpoint == "cancel":
                run = await svc.store.run(rid, identity)
                if run["status"] not in ("completed", "failed", "cancelled"):
                    await svc.runtime.cancel(run, identity)
                result = {"run_id": rid, "status": "cancelled" if run["status"] not in ("completed", "failed") else run["status"]}
            elif endpoint == "events":
                await svc.store.run(rid, identity)
                try:
                    after = max(0, int(request.query_params.get("after", request.headers.get("last-event-id", "0"))))
                except ValueError:
                    raise AIError("事件序号无效") from None

                async def events():
                    cursor, idle = after, 0
                    while not await request.is_disconnected():
                        rows = await svc.store.fetch("SELECT seq,type,data FROM kubedoor_ai_events WHERE run_id=$1::uuid AND seq>$2 ORDER BY seq LIMIT 200", rid, cursor)
                        for event in rows:
                            cursor = event["seq"]
                            yield f"id: {cursor}\ndata: {json.dumps(redact(event), ensure_ascii=False, default=str)}\n\n"
                        current = await svc.store.run(rid, identity)
                        if current["status"] in ("completed", "failed", "cancelled") and cursor >= current["event_seq"]:
                            break
                        idle += 1
                        if idle % 30 == 0:
                            yield ": heartbeat\n\n"
                        await asyncio.sleep(0.5)
                return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
            elif endpoint == "provider-test":
                result = await svc.test_provider((await body(request)).get("provider"))
            elif endpoint == "skills":
                result = {"skills": SKILLS}
            elif endpoint == "skill":
                skill_id = request.path_params["skill_id"]
                if skill_id not in {item["id"] for item in SKILLS}:
                    raise AIError("Skill 不存在", 404, "not_found")
                result = {"id": skill_id, "content": (SKILLS_ROOT / skill_id / "SKILL.md").read_text(encoding="utf-8")}
            elif endpoint == "tools":
                from .gateway import tool_catalog
                result = {"tools": tool_catalog(), "generic_operations": ["api", "kubectl", "istioctl", "diagnostic", "pod_exec"]}
            elif endpoint == "tools-execute":
                data = await body(request)
                result = await svc.mcp_call(identity, data.get("operation"), data.get("arguments", {}), normalize_scope(data.get("scope")), data.get("source", "auto"))
            else:
                raise AIError("接口不存在", 404, "not_found")
            return JSONResponse(redact(plain(result)))
        except AIError as exc:
            error = {"code": exc.code, "message": str(exc)}
            if exc.details:
                error["details"] = redact(exc.details)
            return JSONResponse({"error": error}, status_code=exc.status)
        except (ValueError, asyncpg.DataError):
            return JSONResponse({"error": {"code": "invalid_request", "message": "请求参数或标识无效"}}, status_code=400)

    import asyncpg
    routes = []
    def route(path, endpoint, methods):
        async def named(request):
            request.path_params["endpoint"] = endpoint
            return await handle(request)
        routes.append(Route("/api/ai" + path, named, methods=methods))
    route("/bootstrap", "bootstrap", ["GET"])
    route("/resources", "resources", ["GET"])
    route("/internal/connections-summary", "connections-summary", ["GET"])
    route("/connections", "connections", ["GET"])
    route("/connections/parse", "parse", ["POST"])
    route("/connections/{env}/test", "connection-test", ["POST"])
    route("/connections/{env}", "connection", ["PUT", "DELETE"])
    route("/sessions", "sessions", ["GET", "POST"])
    route("/memories", "memories", ["GET", "POST"])
    route("/memories/{mid}", "memory", ["GET", "PATCH", "DELETE"])
    route("/sessions/{sid}/memory-summary", "memory-summary", ["POST"])
    route("/sessions/{sid}/title", "title", ["POST"])
    route("/sessions/{sid}", "session", ["GET", "PATCH", "DELETE"])
    route("/sessions/{sid}/runs", "runs", ["POST"])
    route("/runs/{rid}/events", "events", ["GET"])
    route("/runs/{rid}/decisions", "decisions", ["POST"])
    route("/runs/{rid}/cancel", "cancel", ["POST"])
    route("/provider/test", "provider-test", ["POST"])
    route("/skills", "skills", ["GET"])
    route("/skills/{skill_id}", "skill", ["GET"])
    route("/tools", "tools", ["GET"])
    route("/tools/execute", "tools-execute", ["POST"])
    routes.append(Route("/health", lambda _: JSONResponse({"ok": True}), methods=["GET"]))
    # Retain the exact external paths without introducing mount redirects.
    routes.extend(streamable.routes)
    routes.extend(legacy.routes)
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(Authentication, token=token)
    if service is not None:
        app.state.service = service
    app.state.mcp = mcp
    return app
