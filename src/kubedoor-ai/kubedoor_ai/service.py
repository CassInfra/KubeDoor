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
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .domain import AIError, CredentialCipher, Identity, Provider, normalize_scope, redact
from .gateway import Gateway, PendingAction, RunContext
from .memories import MemoryLibrary
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
        return {"enabled": True, "username": identity.username, "permission": identity.permission,
                "clusters": sorted(merged.values(), key=lambda r: r["env"]), "skills": SKILLS}

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
            result = {"success": True, "tool_calling": called, "model": provider.model, "base_url": provider.base_url}
            if not called:
                result["warning"] = "连接成功，但未返回有效工具调用；请确认所选模型及网关支持 function/tool calling。"
            return redact(result, (provider.api_key,))
        except Exception as exc:
            raise _provider_failure(exc, provider, stage) from None

    async def new_session(self, identity, title="新会话", key=None):
        title = str(title).strip()[:200] or "新会话"
        return await self.store.one("INSERT INTO kubedoor_ai_sessions(id,username,title,idempotency_key) VALUES($1::uuid,$2,$3,$4) ON CONFLICT(username,idempotency_key) DO UPDATE SET username=EXCLUDED.username RETURNING *", str(uuid.uuid4()), identity.username, title, key)

    async def new_run(self, identity, session_id, scope, origin="chat", skill_ids=None, key=None, memory_ids=None):
        await self.store.session(session_id, identity)
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
            run = await self.store.one("INSERT INTO kubedoor_ai_runs(id,session_id,username,permission,status,scope,origin,skill_ids,idempotency_key) VALUES($1::uuid,$2::uuid,$3,$4,'running',$5,$6,$7,$8) RETURNING *", str(uuid.uuid4()), session_id, identity.username, identity.permission, scope, origin, skills, key)
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

    async def session_detail(self, identity, sid):
        session = await self.store.session(sid, identity)

        async def active_run():
            active = await self.store.one("SELECT id,status,scope,origin,event_seq FROM kubedoor_ai_runs WHERE session_id=$1::uuid AND status IN ('running','waiting_approval') ORDER BY created_at DESC LIMIT 1", sid)
            if active:
                active["pending_actions"] = await self.store.pending(active["id"])
            return active

        # Ownership must be established before starting either data query.
        # History can load while the independent active-run/approval chain runs.
        messages_task = asyncio.create_task(self.store.fetch("SELECT id,role,content,scope,tools,memories,created_at FROM kubedoor_ai_messages WHERE session_id=$1::uuid ORDER BY created_at,id", sid))
        active_task = asyncio.create_task(active_run())
        try:
            messages, active = await asyncio.gather(messages_task, active_task)
        except BaseException:
            # gather does not cancel a sibling when the other query fails.
            # Finish cancellation so disconnected requests leave no DB work behind.
            messages_task.cancel()
            active_task.cancel()
            await asyncio.gather(messages_task, active_task, return_exceptions=True)
            raise
        session["messages"] = messages
        session["active_run"] = active
        return session

    async def decision(self, identity, rid, body):
        run = await self.store.run(rid, identity)
        identity.require_write()
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
        identity.require_write()
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
                    "browserlink": f"/#/workbench/index?ai_session={session['id']}&ai_run={run['id']}"}
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
