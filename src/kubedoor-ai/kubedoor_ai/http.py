from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, AsyncExitStack

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .domain import AIError, authenticate, normalize_scope, redact
from .im import IMBotManager, LegacyWeComBridge, SharedModelManager
from .log_describe import describe as describe_log_sources
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


def _safe_json_response(value):
    """Apply redact(plain(value)) while preserving IMBot non-secret `credentials` dict key."""
    data = plain(value)
    if isinstance(data, dict) and isinstance(data.get("bots"), list):
        bots = []
        for bot in data["bots"]:
            if isinstance(bot, dict) and isinstance(bot.get("credentials"), dict):
                creds = redact(bot["credentials"])
                cleaned = redact({k: v for k, v in bot.items() if k != "credentials"})
                cleaned["credentials"] = creds
                bots.append(cleaned)
            else:
                bots.append(redact(bot))
        rest = redact({k: v for k, v in data.items() if k != "bots"})
        rest["bots"] = bots
        return JSONResponse(rest)
    if isinstance(data, dict) and isinstance(data.get("credentials"), dict) and "platform" in data:
        creds = redact(data["credentials"])
        cleaned = redact({k: v for k, v in data.items() if k != "credentials"})
        cleaned["credentials"] = creds
        return JSONResponse(cleaned)
    return JSONResponse(redact(data))


def create_app(service=None, token=None):
    import uuid
    from .mcp import create_mcp
    token = token if token is not None else os.getenv("AI_INTERNAL_TOKEN", "")
    mcp = create_mcp(lambda: app.state.service, token)
    streamable = mcp.http_app(path="/mcp", transport="http")
    legacy = mcp.http_app(path="/sse", transport="sse")

    @asynccontextmanager
    async def lifespan(app):
        owned = service is None
        app.state.service = service or await Service.open(os.getenv("MASTER_URL", "http://kubedoor-master"), token, os.getenv("AI_ENCRYPTION_KEY", ""))
        if owned:
            app.state.shared_model = SharedModelManager(app.state.service, wire_service=True)
            await app.state.shared_model.load()
            app.state.im_manager = IMBotManager(app.state.service, shared_model=app.state.shared_model)
            await app.state.im_manager.start()
            app.state.wecom_bridge = LegacyWeComBridge(app.state.im_manager, app.state.shared_model)
            app.state.wecom = app.state.wecom_bridge
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(streamable.router.lifespan_context(streamable))
                await stack.enter_async_context(legacy.router.lifespan_context(legacy))
                yield
        finally:
            if owned:
                await app.state.im_manager.close()
                await app.state.service.close()

    async def handle(request: Request):
        svc, identity = request.app.state.service, request.scope["identity"]
        endpoint = request.path_params.get("endpoint", "")
        sid, rid, env = request.path_params.get("sid"), request.path_params.get("rid"), request.path_params.get("env")
        mid = request.path_params.get("mid")
        try:
            if endpoint == "bootstrap":
                result = await svc.bootstrap(identity)
                # 网页「模型设置」只读显示的企微专用模型（不含 API Key）；MCP 的集群列表不需要，只在这里加
                result["shared_model"] = svc.shared_model()
            elif endpoint == "resources":
                result = await svc.resources(identity, request.query_params)
            elif endpoint in ("connections-summary", "connections"):
                result = {"connections": [row for row in await svc.store.summaries() if identity.can_read(row["env"])]}
            elif endpoint == "parse":
                # kubeconfig 直连属于集群接入配置,与 Agent 管理一样只有管理员能改
                identity.require_admin()
                data = await body(request)
                parsed = await svc.parse_connection(data.get("content"), data.get("context"))
                result = {"contexts": parsed.get("contexts", []), "current_context": parsed.get("context"), "requires_context": parsed.get("requires_context", False)}
            elif endpoint == "connection-test":
                data = await body(request)
                tested, _, _ = await svc.test_connection(identity, env, data)
                return JSONResponse(redact(tested), status_code=200 if tested.get("success", True) else 422)
            elif endpoint == "connection-kubeconfig":
                # 修复 Agent:master 用这个集群保存的 kubeconfig 覆盖安装。只在集群内可达(nginx 对外把 /api/ai/internal/
                # 一律 404),返回明文凭据,所以不经过下面的 redact;和保存一样只认系统管理员
                identity.require_admin()
                row = await svc.store.connection(env)
                if not row:
                    raise AIError("这个集群还没有保存 AI Kubeconfig", 404, "connection_missing")
                try:
                    config = svc.gateway.cipher.decrypt(env, bytes(row["encrypted_config"]))
                except Exception:
                    raise AIError("保存的 AI Kubeconfig 无法解密（AI_ENCRYPTION_KEY 变了？），请重新上传", 409, "connection_broken") from None
                return JSONResponse({"env": env, "context": row["context"], "origin": row.get("origin") or "upload",
                                     "content": json.dumps(config)})
            elif endpoint == "connection":
                identity.require_admin()
                if request.method == "DELETE":
                    await svc.store.execute("DELETE FROM kubedoor_ai_connections WHERE env=$1", env)
                    result = {"success": True}
                else:
                    data = await body(request)
                    if not data.get("content") or not data.get("context"):
                        raise AIError("保存连接需要 content 和明确 context")
                    # origin=agent-install：网页安装 Agent 时 master 用 Agent 服务账号自动配置，不覆盖管理员手动上传的
                    origin = "agent-install" if data.get("origin") == "agent-install" else "upload"
                    if origin == "agent-install" and ((await svc.store.connection(env)) or {}).get("origin", "agent-install") != "agent-install":
                        return JSONResponse({"skipped": True, "message": "该集群已有手动上传的 kubeconfig，保留不变"})
                    tested, config, context = await svc.test_connection(identity, env, data)
                    if tested.get("success") is False:
                        return JSONResponse(redact(tested), status_code=422)
                    encrypted = svc.gateway.cipher.encrypt(env, config)
                    # 自动配置只替换自动配置的那份（测试期间管理员刚好上传了，也不覆盖）；手动上传总是替换
                    result = await svc.store.one("INSERT INTO kubedoor_ai_connections(env,encrypted_config,context,updated_by,origin) VALUES($1,$2,$3,$4,$5) ON CONFLICT(env) DO UPDATE SET encrypted_config=EXCLUDED.encrypted_config,context=EXCLUDED.context,revision=kubedoor_ai_connections.revision+1,test_status='success',tested_at=now(),updated_by=EXCLUDED.updated_by,updated_at=now(),origin=EXCLUDED.origin WHERE EXCLUDED.origin='upload' OR kubedoor_ai_connections.origin='agent-install' RETURNING env,context,revision,test_status,tested_at,origin", env, encrypted, context, identity.username, origin)
                    if result is None:
                        return JSONResponse({"skipped": True, "message": "该集群已有手动上传的 kubeconfig，保留不变"})
            elif endpoint == "sessions":
                if request.method == "GET":
                    text = (request.query_params.get("q") or "").strip()[:100]
                    if text:
                        result = {"sessions": await svc.search_sessions(identity, text)}
                    else:
                        result = {"sessions": await svc.store.fetch("SELECT id,title,created_at,updated_at,share_code IS NOT NULL AS shared,public_share_code IS NOT NULL AS public_shared FROM kubedoor_ai_sessions WHERE username=$1 AND channel='web' ORDER BY updated_at DESC", identity.username)}
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
            # 会话分享：开关只有会话本人；分享页任何登录用户凭分享码查看（工具结果按查看人的集群权限）
            elif endpoint == "session-share":
                result = await svc.set_share(identity, sid, (await body(request)).get("enabled"))
            elif endpoint == "share":
                result = await svc.shared_session(identity, request.path_params["code"])
            # 公网分享（Cloudflare Workers）：本人发布 / 取消；管理员配置 Cloudflare
            elif endpoint == "session-public-share":
                if request.method == "POST":
                    result = await svc.publish_public_share(identity, sid)
                else:
                    result = await svc.unpublish_public_share(identity, sid)
            elif endpoint == "public-share-config":
                identity.require_admin()
                if request.method == "GET":
                    result = await svc.public_share_config()
                else:
                    result = await svc.save_public_share_config(identity, await body(request))
            elif endpoint == "public-share-config-test":
                identity.require_admin()
                result = await svc.test_public_share_config(identity, await body(request))
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
                    detail = await svc.session_detail(identity, sid)
                    active = detail["active_run"]
                    if active:
                        await svc.runtime.cancel(await svc.store.run(active["id"], identity), identity)
                    # 先删掉它在 Cloudflare 上的公网快照，删不掉就不删会话；复用详情里的会话行，不重复查归属
                    await svc.discard_public_share(identity, sid, detail)
                    await svc.store.execute("DELETE FROM kubedoor_ai_sessions WHERE id=$1::uuid", sid)
                    if hasattr(svc.runtime.checkpointer, "adelete_thread"):
                        await svc.runtime.checkpointer.adelete_thread(sid)
                    result = {"success": True}
            elif endpoint == "runs":
                data = await body(request)
                message = data.get("message")
                if not isinstance(message, str) or not message.strip() or len(message) > 100000:
                    raise AIError("消息不能为空，最多 100000 字符")
                provider = svc.provider(data.get("provider"))
                key = request.headers.get("idempotency-key")
                # 重新生成 / 编辑后重发：替换最后一轮。同一个幂等键重试时 run 已经建好，不再重复检查
                rewind = None
                if data.get("replace_last") is True and not (key and await svc.store.one(
                        "SELECT id FROM kubedoor_ai_runs WHERE session_id=$1::uuid AND idempotency_key=$2", sid, key)):
                    rewind = await svc.replaceable_turn(identity, sid)
                run, created = await svc.new_run(identity, sid, data.get("scope"), skill_ids=data.get("skill_ids"), key=key, memory_ids=data.get("memory_ids"))
                saved_message = None
                if created:
                    if rewind:
                        await svc.store.execute("UPDATE kubedoor_ai_messages SET replaced_at=now() WHERE session_id=$1::uuid AND run_id=$2::uuid AND replaced_at IS NULL", sid, rewind)
                        run["_rewind"] = rewind
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
            # 用户自己接入的外部 MCP（AI 助手的「MCP 管理」）：任何登录用户，只能管自己账号的
            elif endpoint == "mcp-servers":
                if request.method == "GET":
                    result = await svc.gateway.mcp.list(identity)
                else:
                    result = await svc.gateway.mcp.create(identity, await body(request))
            elif endpoint == "mcp-server-test":
                result = await svc.gateway.mcp.test(identity, await body(request))
            elif endpoint == "mcp-server":
                server_id = request.path_params["mcp_id"]
                if request.method == "PUT":
                    result = await svc.gateway.mcp.update(identity, server_id, await body(request))
                elif request.method == "PATCH":
                    result = await svc.gateway.mcp.patch(identity, server_id, await body(request))
                else:
                    result = await svc.gateway.mcp.delete(identity, server_id)
            elif endpoint == "mcp-server-refresh":
                result = await svc.gateway.mcp.refresh(identity, request.path_params["mcp_id"])
            elif endpoint == "shared-model":
                identity.require_admin()
                if request.method == "GET":
                    result = await request.app.state.shared_model.get_config()
                else:
                    result = await request.app.state.shared_model.save_config(identity, await body(request))
            elif endpoint == "shared-model-test":
                identity.require_admin()
                result = await request.app.state.shared_model.test_config(await body(request))
            elif endpoint == "im-bots":
                identity.require_admin()
                if request.method == "GET":
                    result = {"bots": await request.app.state.im_manager.list_bots()}
                else:
                    result = await request.app.state.im_manager.create_bot(identity, await body(request))
            elif endpoint == "im-bot":
                identity.require_admin()
                bot_id = str(uuid.UUID(request.path_params["bot_id"]))
                if request.method == "GET":
                    result = await request.app.state.im_manager.get_bot(bot_id)
                elif request.method == "PUT":
                    result = await request.app.state.im_manager.update_bot(identity, bot_id, await body(request))
                else:
                    result = await request.app.state.im_manager.delete_bot(bot_id)
            elif endpoint == "im-bot-unbound":
                identity.require_admin()
                bot_id = str(uuid.UUID(request.path_params["bot_id"]))
                result = await request.app.state.im_manager.save_unbound(identity, bot_id, await body(request))
            elif endpoint == "im-users":
                identity.require_admin()
                if request.method == "GET":
                    bot_filter = request.query_params.get("bot_id") or None
                    if bot_filter:
                        bot_filter = str(uuid.UUID(bot_filter))
                    result = {"users": await request.app.state.im_manager.list_users(bot_filter)}
                else:
                    result = await request.app.state.im_manager.save_user(identity, await body(request))
            elif endpoint == "im-user":
                identity.require_admin()
                bot_id = str(uuid.UUID(request.path_params["bot_id"]))
                result = await request.app.state.im_manager.delete_user(bot_id, request.path_params["userid"])
            elif endpoint == "im-user-purge":
                identity.require_admin()
                bot_id = str(uuid.UUID(request.path_params["bot_id"]))
                result = await request.app.state.im_manager.purge_member(bot_id, request.path_params["userid"])
            elif endpoint == "im-user-remark":
                identity.require_admin()
                bot_id = str(uuid.UUID(request.path_params["bot_id"]))
                result = await request.app.state.im_manager.save_remark(bot_id, request.path_params["userid"], await body(request))
            elif endpoint == "im-binding":
                result = await request.app.state.im_manager.binding(identity)
            elif endpoint == "im-binding-code":
                result = await request.app.state.im_manager.create_bind_code(identity)
            elif endpoint == "im-binding-member":
                bot_id = str(uuid.UUID(request.path_params["bot_id"]))
                result = await request.app.state.im_manager.unbind_self(identity, bot_id, request.path_params["userid"])
            elif endpoint == "wecom-config":
                identity.require_admin()
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                if request.method == "GET":
                    result = await wecom_target.admin_config()
                else:
                    result = await wecom_target.save_config(identity, await body(request))
            elif endpoint == "wecom-test":
                identity.require_admin()
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                provider = (await body(request)).get("provider")
                if isinstance(provider, dict) and not provider.get("api_key") and wecom_target.config:
                    provider = {**provider, "api_key": wecom_target.config.get("api_key", "")}
                result = await svc.test_provider(provider)
            elif endpoint == "wecom-users":
                identity.require_admin()
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                if request.method == "GET":
                    result = {"users": await wecom_target.users(),
                              **await wecom_target.unbound_setting()}
                else:
                    result = await wecom_target.save_user(identity, await body(request))
            elif endpoint == "wecom-unbound":
                identity.require_admin()
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                result = await wecom_target.save_unbound(identity, await body(request))
            elif endpoint == "wecom-user":
                identity.require_admin()
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                await wecom_target.delete_user(request.path_params["userid"])
                result = {"success": True}
            elif endpoint == "wecom-user-purge":
                identity.require_admin()
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                result = await wecom_target.purge_member(request.path_params["userid"])
            elif endpoint == "wecom-user-remark":
                identity.require_admin()
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                result = await wecom_target.save_remark(request.path_params["userid"], await body(request))
            # 自助绑定:任何登录用户管理自己账号绑定的企微成员
            elif endpoint == "wecom-binding":
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                result = await wecom_target.binding(identity)
            elif endpoint == "wecom-binding-code":
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                result = await wecom_target.create_bind_code(identity)
            elif endpoint == "wecom-binding-member":
                wecom_target = request.app.state.wecom or request.app.state.wecom_bridge
                result = await wecom_target.unbind_self(identity, request.path_params["userid"])
            elif endpoint == "skills":
                result = {"skills": SKILLS}
            elif endpoint == "skill":
                skill_id = request.path_params["skill_id"]
                if skill_id not in {item["id"] for item in SKILLS}:
                    raise AIError("Skill 不存在", 404, "not_found")
                result = {"id": skill_id, "content": (SKILLS_ROOT / skill_id / "SKILL.md").read_text(encoding="utf-8")}
            elif endpoint == "tools":
                from .gateway import tool_catalog
                result = {"tools": tool_catalog(), "generic_operations": ["api", "kubectl", "istioctl", "diagnostic", "pod_exec", "mysql", "redis", "elasticsearch"]}
            elif endpoint == "tools-execute":
                data = await body(request)
                result = await svc.mcp_call(identity, data.get("operation"), data.get("arguments", {}), normalize_scope(data.get("scope")), data.get("source", "auto"))
            # 历史日志配置页的「AI 生成说明」：只有系统管理员，用公共默认模型（没配好 409），逻辑在 log_describe.py
            elif endpoint == "log-describe":
                identity.require_admin()
                data = await body(request)
                result = await describe_log_sources(data, svc.provider({"source": "shared"}), svc.runtime.model_factory)
            else:
                raise AIError("接口不存在", 404, "not_found")
            return _safe_json_response(result)
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
    route("/internal/connections/{env}/kubeconfig", "connection-kubeconfig", ["GET"])
    route("/connections", "connections", ["GET"])
    route("/connections/parse", "parse", ["POST"])
    route("/connections/{env}/test", "connection-test", ["POST"])
    route("/connections/{env}", "connection", ["PUT", "DELETE"])
    route("/sessions", "sessions", ["GET", "POST"])
    route("/memories", "memories", ["GET", "POST"])
    route("/memories/{mid}", "memory", ["GET", "PATCH", "DELETE"])
    route("/sessions/{sid}/memory-summary", "memory-summary", ["POST"])
    route("/sessions/{sid}/title", "title", ["POST"])
    route("/sessions/{sid}/share", "session-share", ["PUT"])
    route("/sessions/{sid}/public-share", "session-public-share", ["POST", "DELETE"])
    route("/shares/{code}", "share", ["GET"])
    route("/public-share-config", "public-share-config", ["GET", "PUT"])
    route("/public-share-config/test", "public-share-config-test", ["POST"])
    route("/sessions/{sid}", "session", ["GET", "PATCH", "DELETE"])
    route("/sessions/{sid}/runs", "runs", ["POST"])
    route("/runs/{rid}/events", "events", ["GET"])
    route("/runs/{rid}/decisions", "decisions", ["POST"])
    route("/runs/{rid}/cancel", "cancel", ["POST"])
    route("/provider/test", "provider-test", ["POST"])
    route("/mcp-servers", "mcp-servers", ["GET", "POST"])
    route("/mcp-servers/test", "mcp-server-test", ["POST"])
    route("/mcp-servers/{mcp_id}", "mcp-server", ["PUT", "PATCH", "DELETE"])
    route("/mcp-servers/{mcp_id}/refresh", "mcp-server-refresh", ["POST"])
    route("/shared-model", "shared-model", ["GET", "PUT"])
    route("/shared-model/test", "shared-model-test", ["POST"])
    route("/im/bots", "im-bots", ["GET", "POST"])
    route("/im/bots/{bot_id}", "im-bot", ["GET", "PUT", "DELETE"])
    route("/im/bots/{bot_id}/unbound", "im-bot-unbound", ["PUT"])
    route("/im/users", "im-users", ["GET", "POST"])
    route("/im/users/{bot_id}/{userid}", "im-user", ["DELETE"])
    route("/im/users/{bot_id}/{userid}/purge", "im-user-purge", ["POST"])
    route("/im/users/{bot_id}/{userid}/remark", "im-user-remark", ["PUT"])
    route("/im/binding", "im-binding", ["GET"])
    route("/im/binding/code", "im-binding-code", ["POST"])
    route("/im/binding/{bot_id}/{userid}", "im-binding-member", ["DELETE"])
    route("/wecom", "wecom-config", ["GET", "PUT"])
    route("/wecom/test", "wecom-test", ["POST"])
    route("/wecom/users", "wecom-users", ["GET", "POST"])
    route("/wecom/unbound", "wecom-unbound", ["PUT"])
    route("/wecom/users/{userid}", "wecom-user", ["DELETE"])
    route("/wecom/users/{userid}/purge", "wecom-user-purge", ["POST"])
    route("/wecom/users/{userid}/remark", "wecom-user-remark", ["PUT"])
    route("/wecom/binding", "wecom-binding", ["GET"])
    route("/wecom/binding/code", "wecom-binding-code", ["POST"])
    route("/wecom/binding/{userid}", "wecom-binding-member", ["DELETE"])
    route("/skills", "skills", ["GET"])
    route("/skills/{skill_id}", "skill", ["GET"])
    route("/tools", "tools", ["GET"])
    route("/tools/execute", "tools-execute", ["POST"])
    route("/log-sources/describe", "log-describe", ["POST"])
    async def setup_health(request):
        try:
            await asyncio.wait_for(request.app.state.service.store.one('SELECT 1 AS ready'), 5)
            return JSONResponse({'ok': True, 'installation_id': os.getenv('KUBEDOOR_SETUP_ID', '')})
        except Exception:
            return JSONResponse({'ok': False, 'message': '数据库连接检查失败'}, status_code=503)

    routes.append(Route('/api/ai/internal/setup-health', setup_health, methods=['GET']))
    routes.append(Route("/health", lambda _: JSONResponse({"ok": True}), methods=["GET"]))
    # Retain the exact external paths without introducing mount redirects.
    routes.extend(streamable.routes)
    routes.extend(legacy.routes)
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(Authentication, token=token)
    if service is not None:
        app.state.service = service
        app.state.wecom = None
        app.state.shared_model = SharedModelManager(service, wire_service=False)
        app.state.im_manager = IMBotManager(service, shared_model=app.state.shared_model)
        app.state.wecom_bridge = LegacyWeComBridge(app.state.im_manager, app.state.shared_model)
    app.state.mcp = mcp
    return app
