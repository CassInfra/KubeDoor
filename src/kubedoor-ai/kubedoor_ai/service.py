from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import AsyncExitStack
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

import asyncpg
import httpx
import openai
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .domain import AIError, CredentialCipher, Identity, Provider, normalize_scope, redact
from .gateway import Gateway, PendingAction, RunContext
from .memories import MemoryLibrary
from .models import without_thinking
from .runtime import Runtime, SKILLS, make_model
from .storage import Store, pg_dsn


LOGGER = logging.getLogger(__name__)
PROBE_TOOL = {"name": "kubedoor_connection_probe", "description": "验证模型的工具调用能力", "parameters": {"type": "object", "properties": {}, "required": []}}


def _status_of(exc):
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _upstream_fields(exc):
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return {}
    error = body.get("error", body)
    if isinstance(error, str):
        return {"message": error}
    return {k: error[k] for k in ("message", "code", "param") if isinstance(error, dict) and isinstance(error.get(k), str)}


def _tool_parameter_error(exc):
    if _status_of(exc) not in (400, 422):
        return None
    fields = _upstream_fields(exc)
    param, message, code = fields.get("param", "").lower(), fields.get("message", "").lower(), fields.get("code", "").lower()
    if param == "tool_choice":
        return "tool_choice"
    unsupported = any(word in message or word in code for word in ("unsupported", "not support", "doesn't support", "not allowed", "not implemented"))
    if unsupported and "tool_choice" in message:
        return "tool_choice"
    if unsupported and (param in ("tools", "functions", "function_call") or any(word in message for word in ("tools", "function calling", "function_call"))):
        return "tools"
    return None


def _safe_provider_text(value, provider, limit=400):
    # Never expose credentials or query strings echoed inside upstream messages.
    value = "".join(c if ord(c) >= 32 else " " for c in value)

    def without_query(match):
        try:
            parsed = urlsplit(match.group())
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if parsed.port is not None:
                host += f":{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return "[URL]"

    value = re.sub(r"https?://[^\s<>\"']+", without_query, value)
    key = provider.api_key if provider else ""
    secrets = (key, quote(key, safe=""), quote_plus(key)) if key else ()
    return redact(value, secrets)[:limit]


def _provider_failure(exc, provider=None, stage="request"):
    status, fields = _status_of(exc), _upstream_fields(exc)
    if isinstance(exc, AIError):
        code, message, response_status = exc.code, str(exc), exc.status
    else:
        response_status = 422
        if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException, TimeoutError)):
            code, message = "provider_timeout", "连接模型服务超时，请检查 AI 服务到模型地址的网络连通性及服务响应时间。"
        elif isinstance(exc, (openai.APIConnectionError, httpx.NetworkError, OSError)):
            code, message = "provider_connection_failed", "AI 服务无法连接模型地址，请检查 DNS、TLS、代理和网络连通性。"
        elif status in (401, 403):
            code, message = "provider_auth_failed", f"模型服务鉴权失败（HTTP {status}），请检查 API key 和模型访问权限。"
        elif status == 404:
            code, message = "provider_not_found", "模型服务返回 HTTP 404，可能是模型名或 API 地址不匹配；请核对模型 ID 和 Chat Completions API 基址。"
        elif status == 405:
            code, message = "provider_address_invalid", "模型服务返回 HTTP 405，请确认 Base URL 对应 Chat Completions API 基址。"
        elif status == 429:
            code, message = "provider_rate_limited", "模型服务限流或额度不足（HTTP 429），请检查额度后重试。"
        elif status is not None and status >= 500:
            code, message = "provider_unavailable", f"模型服务暂不可用（HTTP {status}），请检查供应商或网关服务。"
        elif _tool_parameter_error(exc):
            code, message = "provider_tool_calling_unsupported", "模型服务拒绝工具调用参数，请确认该模型及网关支持 OpenAI-compatible 工具调用。"
        elif status in (400, 422):
            code, message = "provider_request_rejected", f"模型服务拒绝测试请求（HTTP {status}），请核对服务要求、模型名及参数兼容性。"
        elif stage == "initialize":
            code, message = "provider_configuration_invalid", "模型客户端初始化失败，请检查地址、API key 和模型配置。"
        elif isinstance(exc, (json.JSONDecodeError, ValueError)):
            code, message = "provider_response_invalid", "模型服务返回内容无法按 Chat Completions 协议解析，请检查 API 地址及兼容性。"
        else:
            code, message = "provider_test_failed", "模型测试失败，请使用诊断 ID 查看 AI 服务日志。"
    diagnostic_id = uuid.uuid4().hex
    details = {"diagnostic_id": diagnostic_id}
    if status is not None:
        details["upstream_status"] = status
    for field in ("code", "param", "message"):
        if fields.get(field):
            details[f"upstream_{field}"] = _safe_provider_text(fields[field], provider)
    if details.get("upstream_message"):
        message += " 服务提示：" + details["upstream_message"]
    message += f"（诊断 ID：{diagnostic_id}）"
    log = {"diagnostic_id": diagnostic_id, "code": code, "stage": stage, "exception": type(exc).__name__, "upstream_status": status}
    if provider:
        parsed = urlsplit(provider.base_url)
        log.update(host=_safe_provider_text(parsed.hostname or "", provider, 200), path=_safe_provider_text(parsed.path, provider, 200), model=_safe_provider_text(provider.model, provider, 256))
    for field in ("upstream_code", "upstream_param"):
        if details.get(field):
            log[field] = details[field]
    # Do not log exc/traceback, request body, query, headers or upstream message.
    LOGGER.warning("模型连接测试失败 %s", json.dumps(log, ensure_ascii=False))
    return AIError(message, response_status, code, details)


def search_snippet(content, text, radius=40):
    """命中关键字前后各 radius 个字，换行压成空格"""
    if not isinstance(content, str) or not content:
        return ""
    flat = " ".join(content.split())
    index = flat.lower().find(text.lower())
    if index < 0:
        return flat[:radius * 2] + ("…" if len(flat) > radius * 2 else "")
    start, end = max(0, index - radius), min(len(flat), index + len(text) + radius)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


class Service:
    def __init__(self, store, gateway, runtime, stack=None):
        self.store, self.gateway, self.runtime, self.stack = store, gateway, runtime, stack
        self.memory_library = MemoryLibrary(store, model_factory=lambda provider: self.runtime.model_factory(provider))

    @classmethod
    async def open(cls, master_url, internal_token, encryption_key):
        if not internal_token:
            raise RuntimeError("AI_INTERNAL_TOKEN 不能为空")
        cipher = CredentialCipher(encryption_key)
        store = await Store.open()
        stack = AsyncExitStack()
        try:
            saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(pg_dsn()))
            await saver.setup()
            gateway = Gateway(store, cipher, master_url, internal_token)
            service = cls(store, gateway, Runtime(store, gateway, saver), stack)
            await store.recover()
            return service
        except BaseException:
            await stack.aclose()
            await store.close()
            raise

    async def bootstrap(self, identity):
        summaries = {r["env"]: r for r in await self.store.summaries()}
        try:
            upstream = await self.gateway.master(identity, "GET", "/api/ai/internal/bootstrap")
            clusters = upstream.get("clusters", [])
        except AIError:
            clusters = []
        merged = {row["env"]: {**row, **summaries.get(row["env"], {}), "configured": row["env"] in summaries} for row in clusters}
        for env, summary in summaries.items():
            merged.setdefault(env, {**summary, "online": False, "ai_tools": False})
        # 对当前用户为「无」权限的集群完全不可见;permission 为该集群的有效权限,前端据此决定能否批准修改
        visible = [{**row, "permission": identity.permission_for(row["env"])}
                   for row in merged.values() if identity.can_read(row["env"])]
        return {"enabled": True, "username": identity.username, "permission": identity.permission,
                "role": identity.permission, "is_admin": identity.is_admin,
                "clusters": sorted(visible, key=lambda r: r["env"]), "skills": SKILLS}

    async def ensure_env(self, identity, env):
        cluster = next((r for r in (await self.bootstrap(identity))["clusters"] if r["env"] == env), None)
        if cluster is None:
            raise AIError("集群未登记或无法确认集群身份", 404, "cluster_not_found")
        return cluster

    @staticmethod
    def capabilities(cluster):
        online, upgraded = bool(cluster.get("online")), bool(cluster.get("ai_tools"))
        return {"kubedoor": {"stored": True, "historical": True, "master": True, "metrics": True, "live": online},
                "agent": {"available": online and upgraded, "online": online, "ai_tools": upgraded},
                "direct": {"configured": bool(cluster.get("configured")), "context": cluster.get("context"), "test_status": cluster.get("test_status")}}

    async def test_provider(self, raw):
        provider, stage = None, "validate"
        try:
            provider = Provider.parse(raw)
            stage = "initialize"
            model = make_model(provider)
            prompt = [HumanMessage(content="请调用 kubedoor_connection_probe 工具进行连接验证。")]
            stage = "request"
            # Match runtime behavior: compatible providers need not implement
            # forced/auto tool_choice. Verify the actual named call instead.
            async with asyncio.timeout(50):
                response = await model.bind_tools([PROBE_TOOL]).ainvoke(prompt, config={"callbacks": []})
            called = any(isinstance(call, dict) and call.get("name") == PROBE_TOOL["name"] and isinstance(call.get("args"), dict) for call in (getattr(response, "tool_calls", None) or []))
            # 没有用量时压缩只能靠近似计数（不含推理内容），用量统计也是 0；少数网关不支持 stream_options
            usage_reported = bool((getattr(response, "usage_metadata", None) or {}).get("input_tokens"))
            result = {"success": True, "tool_calling": called, "usage_reported": usage_reported, "model": provider.model, "base_url": provider.base_url}
            problems = []
            if not called:
                problems.append("未返回有效工具调用，请确认所选模型及网关支持 function/tool calling")
            if not usage_reported:
                problems.append("没有返回 token 用量（可能不支持 stream_options），上下文压缩只能按估算触发，用量统计会缺失")
            if problems:
                result["warning"] = "连接成功，但" + "；".join(problems) + "。"
            return redact(result, (provider.api_key,))
        except Exception as exc:
            raise _provider_failure(exc, provider, stage) from None

    async def new_session(self, identity, title="新会话", key=None):
        title = str(title).strip()[:200] or "新会话"
        return await self.store.one("INSERT INTO kubedoor_ai_sessions(id,username,title,idempotency_key) VALUES($1::uuid,$2,$3,$4) ON CONFLICT(username,idempotency_key) DO UPDATE SET username=EXCLUDED.username RETURNING *", str(uuid.uuid4()), identity.username, title, key)

    async def generate_session_title(self, identity, session_id, message, provider_raw):
        """Generate a short title independently of the active conversation run.

        This endpoint is deliberately best-effort: the chat request has already
        been accepted before this method is called, and a title failure must not
        affect the run or its stream. The conditional update also preserves a
        title that the user renamed while the model was responding.
        """
        if not isinstance(message, str) or not message.strip() or len(message) > 100000:
            raise AIError("首条消息不能为空", 400, "invalid_title_request")
        session = await self.store.session(session_id, identity)
        provider = Provider.parse(provider_raw)
        prompt = (
            "使用四到五个字直接返回这句话的简要主题，不要解释、不要标点、"
            "不要语气词、不要多余文本，不要加粗，如果没有主题，请直接返回“闲聊”"
        )
        try:
            model = without_thinking(self.runtime.model_factory(provider))
            response = await asyncio.wait_for(
                model.ainvoke([
                    SystemMessage(content=prompt),
                    HumanMessage(content=message.strip()),
                ], config={"callbacks": []}),
                timeout=20,
            )
        except Exception:
            return {"id": session_id, "title": session["title"], "generated": False}
        value = getattr(response, "content", "")
        if isinstance(value, list):
            value = "".join(
                item.get("text", "") for item in value
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        if not isinstance(value, str):
            return {"id": session_id, "title": session["title"], "generated": False}
        # Keep the model's requested plain-text shape even when a provider adds
        # quotes, Markdown fences, punctuation, or a short explanation.
        title = value.replace("```", "").strip().splitlines()[0].strip()
        title = re.sub(r"^(?:主题|标题)\s*[:：]\s*", "", title)
        title = title.strip("\"'“”‘’* ")
        title = re.sub(r"[\s，。！？；：:,.!?;、]+", "", title)
        if not title:
            title = "闲聊"
        title = title[:20]
        row = await self.store.one(
            "UPDATE kubedoor_ai_sessions SET title=$2,updated_at=now() "
            "WHERE id=$1::uuid AND username=$3 AND title='新会话' "
            "RETURNING id,title,updated_at",
            session_id, title, identity.username,
        )
        if not row:
            current = await self.store.session(session_id, identity)
            return {"id": session_id, "title": current["title"], "generated": False}
        return {**row, "generated": True}

    async def new_run(self, identity, session_id, scope, origin="chat", skill_ids=None, key=None, memory_ids=None, channel="web"):
        if channel == "web":
            await self.store.session(session_id, identity)
        else:
            await self.store.session(session_id, identity, channel=channel)
        scope = normalize_scope(scope)
        cluster = await self.ensure_env(identity, scope["env"])
        skills = skill_ids or []
        if not isinstance(skills, list) or any(s not in {r["id"] for r in SKILLS} for s in skills):
            raise AIError("包含未知 Skill")
        if key:
            existing = await self.store.one("SELECT * FROM kubedoor_ai_runs WHERE session_id=$1::uuid AND idempotency_key=$2", session_id, key)
            if existing:
                return existing, False
        # Capture only explicitly selected memories before registering a run.
        # Their contents belong to the user message/checkpoint, not a global
        # system prompt or a separate run-level memory snapshot.
        memories = await self.memory_library.resolve([] if memory_ids is None else memory_ids)
        try:
            run = await self.store.one("INSERT INTO kubedoor_ai_runs(id,session_id,username,permission,status,scope,origin,skill_ids,idempotency_key,rewindable) VALUES($1::uuid,$2::uuid,$3,$4,'running',$5,$6,$7,$8,true) RETURNING *", str(uuid.uuid4()), session_id, identity.username, identity.permission_for(scope["env"]), scope, origin, skills, key)
        except asyncpg.UniqueViolationError:
            if key:
                existing = await self.store.one("SELECT * FROM kubedoor_ai_runs WHERE session_id=$1::uuid AND idempotency_key=$2", session_id, key)
                if existing:
                    return existing, False
            raise AIError("该会话有正在运行或等待批准的任务", 409, "run_active") from None
        run["capabilities"] = self.capabilities(cluster)
        if memories:
            run["_memories"] = memories
        return run, True

    async def search_sessions(self, identity, text):
        """按标题和消息内容搜索本人的网页会话，附上最近一条命中消息的片段"""
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await self.store.fetch(
            "SELECT s.id,s.title,s.created_at,s.updated_at,"
            "(SELECT m.content FROM kubedoor_ai_messages m WHERE m.session_id=s.id AND m.replaced_at IS NULL AND m.content ILIKE $2 "
            "ORDER BY m.created_at DESC LIMIT 1) AS matched "
            "FROM kubedoor_ai_sessions s WHERE s.username=$1 AND s.channel='web' AND (s.title ILIKE $2 OR EXISTS "
            "(SELECT 1 FROM kubedoor_ai_messages m WHERE m.session_id=s.id AND m.replaced_at IS NULL AND m.content ILIKE $2)) "
            "ORDER BY s.updated_at DESC LIMIT 50", identity.username, pattern)
        for row in rows:
            row["snippet"] = search_snippet(row.pop("matched", None), text)
        return rows

    async def replaceable_turn(self, identity, sid):
        """重新生成 / 编辑后重发前的检查，返回被替换那一轮的 run id。

        只能替换最后一轮，而且这一轮不能已经改过集群（AI 会忘掉这次修改、可能再做一遍），
        也不能触发过上下文压缩（摘要事件指向的位置会错开）。
        """
        await self.store.session(sid, identity)
        if await self.store.one("SELECT id FROM kubedoor_ai_runs WHERE session_id=$1::uuid AND status IN ('running','waiting_approval') LIMIT 1", sid):
            raise AIError("会话里还有进行中或等待批准的任务，先停止或等它结束", 409, "run_active")
        last = await self.store.one(
            "SELECT m.run_id,r.usage,r.rewindable FROM kubedoor_ai_messages m JOIN kubedoor_ai_runs r ON r.id=m.run_id "
            "WHERE m.session_id=$1::uuid AND m.role='user' AND m.replaced_at IS NULL ORDER BY m.created_at DESC,m.id DESC LIMIT 1", sid)
        if not last:
            raise AIError("没有可以重新生成的消息", 409, "nothing_to_replace")
        if not last["rewindable"]:
            raise AIError("这条消息来自升级前的版本，不能重新生成，请直接继续提问", 409, "turn_not_rewindable")
        if await self.store.one(
                "SELECT id FROM kubedoor_ai_actions WHERE run_id=$1::uuid AND read_only=false AND "
                "(state NOT IN ('rejected','failed','pending') OR result->'error'->>'code' IN ('partial_failure','outcome_unknown')) LIMIT 1",
                last["run_id"]):
            raise AIError("这一轮已经执行过修改操作，重新生成会让 AI 忘掉这次修改、可能重复执行；请直接继续提问", 409, "turn_has_writes")
        if ((last["usage"] or {}).get("summary_calls") or 0) > 0:
            raise AIError("这一轮触发了上下文压缩，不能重新生成，请直接继续提问", 409, "turn_compacted")
        return last["run_id"]

    async def session_detail(self, identity, sid):
        session = await self.store.session(sid, identity)

        async def active_run():
            active = await self.store.one("SELECT id,status,scope,origin,event_seq FROM kubedoor_ai_runs WHERE session_id=$1::uuid AND status IN ('running','waiting_approval') ORDER BY created_at DESC LIMIT 1", sid)
            if active:
                active["pending_actions"] = await self.store.pending(active["id"])
            return active

        # Ownership must be established before starting either data query.
        # History can load while the independent active-run/approval chain runs.
        messages_task = asyncio.create_task(self.store.fetch("SELECT id,role,content,scope,tools,memories,created_at FROM kubedoor_ai_messages WHERE session_id=$1::uuid AND replaced_at IS NULL ORDER BY created_at,id", sid))
        active_task = asyncio.create_task(active_run())
        # 最近一轮的用量：网页按「最近一次输入 ÷ 模型窗口」显示上下文占用
        usage_task = asyncio.create_task(self.store.one("SELECT usage FROM kubedoor_ai_runs WHERE session_id=$1::uuid AND usage IS NOT NULL ORDER BY created_at DESC LIMIT 1", sid))
        try:
            messages, active, latest = await asyncio.gather(messages_task, active_task, usage_task)
        except BaseException:
            # gather does not cancel a sibling when the other query fails.
            # Finish cancellation so disconnected requests leave no DB work behind.
            for task in (messages_task, active_task, usage_task):
                task.cancel()
            await asyncio.gather(messages_task, active_task, usage_task, return_exceptions=True)
            raise
        session["messages"] = messages
        session["active_run"] = active
        session["usage"] = (latest or {}).get("usage")
        return session

    async def decision(self, identity, rid, body):
        run = await self.store.run(rid, identity)
        identity.require_write(run["scope"]["env"])
        decision, action_id = body.get("decision"), body.get("action_id")
        if decision not in ("approve", "reject") or not action_id:
            raise AIError("请提供 action_id 和 approve/reject")
        action = await self.store.one("SELECT * FROM kubedoor_ai_actions WHERE id=$1::uuid AND run_id=$2::uuid", action_id, rid)
        if not action:
            raise AIError("批准操作不存在", 404, "not_found")
        if action["state"] != "pending":
            return {"run_id": rid, "status": run["status"], "action_status": action["state"]}
        if run["status"] != "waiting_approval" or run["cancel_requested"]:
            raise AIError("任务没有等待批准或已经停止", 409, "not_pending")
        valid = await self.store.one("SELECT id FROM kubedoor_ai_actions WHERE id=$1::uuid AND expires_at>now()", action_id)
        if not valid:
            raise AIError("批准已过期，请停止本轮后重新生成操作", 409, "approval_expired")
        if action["source"] == "direct":
            connection = await self.store.connection(run["scope"]["env"])
            if not connection or connection["revision"] != action["connection_revision"] or self.gateway.connection_fingerprint(connection) != action["preview"].get("credential_fingerprint"):
                raise AIError("kubeconfig 已变更，请重新生成操作", 409, "connection_changed")
        if run["origin"] == "mcp":
            changed = await self.store.one("UPDATE kubedoor_ai_actions SET state=$2 WHERE id=$1::uuid AND state='pending' RETURNING id", action_id, "approved" if decision == "approve" else "rejected")
            if not changed:
                return {"run_id": rid, "status": run["status"]}
            await self.store.status(rid, "running")
            ctx = RunContext(run, identity)
            self.runtime.contexts[rid] = ctx
            task = asyncio.create_task(self.finish_mcp(ctx, action, decision))
            self.runtime.tasks[rid] = task
            task.add_done_callback(lambda _: self.runtime.tasks.pop(rid, None))
        else:
            provider = Provider.parse(body.get("provider"))
            if not action.get("interrupt_id"):
                raise AIError("批准点尚未写入，请稍后重试", 409, "checkpoint_pending")
            if rid in self.runtime.tasks:
                raise AIError("任务正在恢复，请稍后重试", 409, "run_active")
            run["capabilities"] = self.capabilities(await self.ensure_env(identity, run["scope"]["env"]))
            self.runtime.start(run, identity, provider, resume={action["interrupt_id"]: {"decision": decision, "action_id": action_id}})
        return {"run_id": rid, "status": "running"}

    async def finish_mcp(self, ctx, action, decision):
        run, status = ctx.run, "completed"
        try:
            if decision == "approve":
                result = await self.gateway.dispatch(ctx, action)
            else:
                result = {"success": False, "error": {"code": "rejected", "message": "用户拒绝了该操作"}}
                await self.store.execute("UPDATE kubedoor_ai_actions SET result=$2 WHERE id=$1::uuid", action["id"], result)
        except asyncio.CancelledError:
            status = "cancelled"
            result = {"success": False, "error": {"code": "cancelled", "message": "任务已停止，已提交的修改请查询实际状态"}}
        except Exception as exc:
            status = "failed"
            result = {"success": False, "error": {"code": getattr(exc, "code", "tool_failed"), "message": str(exc) if isinstance(exc, AIError) else "工具执行失败，请核验实际状态"}}
        finally:
            await self.gateway.close_context(ctx)
            self.runtime.contexts.pop(run["id"], None)
        tools = await self.store.fetch("SELECT id AS action_id,operation AS name,source,state,arguments,preview,result FROM kubedoor_ai_actions WHERE run_id=$1::uuid ORDER BY created_at", run["id"])
        saved = await self.store.message(run, "assistant", json.dumps(result, ensure_ascii=False), tools)
        await self.store.event(run["id"], "message", saved)
        await self.store.status(run["id"], status)
        await self.store.event(run["id"], "done", {"status": status})

    async def parse_connection(self, content, context=None):
        from kubedoor_tools import parse_kubeconfig
        if not isinstance(content, str) or not content or len(content.encode()) > 2 * 1024 * 1024:
            raise AIError("kubeconfig 不能为空，最大 2 MiB")
        try:
            return parse_kubeconfig(content, context)
        except Exception as exc:
            raise AIError(f"kubeconfig 无效：{type(exc).__name__}") from None

    async def test_connection(self, identity, env, body):
        from kubedoor_tools import KubernetesExecutor
        identity.require_admin()
        await self.ensure_env(identity, env)
        if body.get("content") is not None:
            parsed = await self.parse_connection(body["content"], body.get("context"))
            config, context = parsed.get("config"), parsed.get("context")
            if not config or not context:
                raise AIError("请选择 kubeconfig context")
        else:
            row = await self.store.connection(env)
            if not row:
                raise AIError("尚未配置 kubeconfig", 404, "connection_missing")
            config = self.gateway.cipher.decrypt(env, bytes(row["encrypted_config"]))
            context = body.get("context") or row["context"]
        executor = KubernetesExecutor(config, context)
        try:
            result = await executor.test_connection()
            return result, config, context
        finally:
            await executor.close()

    async def resources(self, identity, query):
        env, kind = query.get("env"), query.get("kind")
        if not env or kind not in ("namespaces", "deployments", "pods"):
            raise AIError("资源查询需要 env 与 namespaces/deployments/pods 类型")
        ns = query.get("namespace") or None
        deployment_name = query.get("deployment") or None
        if kind == "deployments" and not ns:
            raise AIError("Deployment 菜单需要先选择具体命名空间")
        if kind == "pods" and (not ns or not deployment_name):
            raise AIError("Pod 菜单需要先选择具体命名空间和 Deployment")
        scope = normalize_scope({"env": env, "namespace": ns,
                                 "deployment": {"namespace": query.get("deployment_namespace") or ns, "name": deployment_name} if kind == "pods" else None})
        await self.ensure_env(identity, env)
        ctx = RunContext({"scope": scope}, identity)
        try:
            source = "direct" if await self.store.connection(env) else "agent"

            async def read(path, params=None):
                nonlocal source
                action = {"source": source, "operation": "api", "arguments": {"method": "GET", "path": path, "query": params or {}}, "call_id": "resources:" + uuid.uuid4().hex, "read_only": True}
                try:
                    result = await self.gateway.execute_action(ctx, action)
                except Exception:
                    if source != "direct":
                        raise
                    result = {"success": False}
                if not isinstance(result, dict):
                    result = {"success": False, "error": {"code": "resource_response_invalid"}}
                if source == "direct" and result.get("success") is False:
                    source = "agent"
                    action["source"] = "agent"
                    result = await self.gateway.execute_action(ctx, action)
                if not isinstance(result, dict):
                    raise AIError("资源查询返回了无效的执行器结果", 502, "resource_response_invalid")
                if result.get("success") is False:
                    raise AIError("资源查询失败，请检查所选连接", 502, "resource_query_failed")
                data = result.get("data", result)
                if not isinstance(data, dict):
                    raise AIError("资源查询返回了无效的 Kubernetes 对象结构", 502, "resource_response_invalid")
                if data.get("omitted"):
                    raise AIError("资源响应超出大小限制，请缩小查询范围后重试", 502, "resource_list_truncated")
                return data

            params = {"limit": "100"}
            if kind == "namespaces":
                path = "/api/v1/namespaces"
            elif kind == "deployments":
                path = f"/apis/apps/v1/namespaces/{quote(ns, safe='')}/deployments"
            else:
                deployment = await read(f"/apis/apps/v1/namespaces/{quote(ns, safe='')}/deployments/{quote(deployment_name, safe='')}")
                selector = deployment.get("spec", {}).get("selector", {})
                labels = [f"{k}={v}" for k, v in selector.get("matchLabels", {}).items()]
                for expression in selector.get("matchExpressions", []):
                    key, op, values = expression["key"], expression["operator"], expression.get("values", [])
                    labels.append(f"{key} {op.lower()} ({','.join(values)})" if op in ("In", "NotIn") else key if op == "Exists" else f"!{key}")
                if not labels:
                    raise AIError("Deployment 返回了空资源选择器，不能确定其 Pod", 502, "resource_response_invalid")
                params["labelSelector"] = ",".join(labels)
                path = f"/api/v1/namespaces/{quote(ns, safe='')}/pods"
            items, truncated = [], False
            while True:
                data = await read(path, params)
                if not isinstance(data.get("items"), list):
                    raise AIError("Kubernetes 列表响应缺少 items，请检查执行器返回结果", 502, "resource_response_invalid")
                for resource in data.get("items", []):
                    metadata = resource.get("metadata", {})
                    item = {"name": metadata.get("name", "")}
                    if metadata.get("namespace"):
                        item["namespace"] = metadata["namespace"]
                    items.append(item)
                continuation = data.get("metadata", {}).get("continue")
                if not continuation:
                    break
                if len(items) >= 50000:
                    truncated = True
                    break
                params["continue"] = continuation
            return {"items": sorted(items, key=lambda r: (r.get("namespace", ""), r["name"])), "truncated": truncated, "source": source}
        finally:
            await self.gateway.close_context(ctx)

    async def mcp_call(self, identity, operation, arguments, scope, source="auto"):
        session = await self.new_session(identity, f"MCP · {operation}")
        run, _ = await self.new_run(identity, session["id"], scope, origin="mcp")
        await self.store.message(run, "user", json.dumps({"operation": operation, "source": source, "arguments": redact(arguments)}, ensure_ascii=False))
        ctx = RunContext(run, identity)
        try:
            result = await self.gateway.invoke(ctx, operation, arguments, source, mcp=True)
            message = await self.store.message(run, "assistant", json.dumps(result, ensure_ascii=False), [result])
            await self.store.event(run["id"], "message", message)
            await self.store.status(run["id"], "completed")
            await self.store.event(run["id"], "done", {"status": "completed"})
            return result
        except PendingAction as pending:
            await self.store.event(run["id"], "approval", pending.action)
            await self.store.status(run["id"], "waiting_approval")
            return {"success": False, "pending": True, "action_id": pending.action["action_id"], "run_id": run["id"], "session_id": session["id"], "preview": pending.action,
                    "browserlink": f"/#/monit/overview?ai_session={session['id']}&ai_run={run['id']}"}
        except Exception:
            await self.store.status(run["id"], "failed")
            await self.store.event(run["id"], "done", {"status": "failed"})
            raise
        finally:
            await self.gateway.close_context(ctx)

    async def close(self):
        await self.runtime.close()
        await self.gateway.close()
        if self.stack:
            await self.stack.aclose()
        await self.store.close()
