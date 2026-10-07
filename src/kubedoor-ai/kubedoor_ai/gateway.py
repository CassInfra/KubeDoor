from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote

import httpx
from langgraph.types import interrupt

from .domain import AIError, CredentialCipher, Identity, beijing_now, redact, utc_now
from .catalogue import EXTRA_CATALOG, EXTRA_SCHEMAS, build_request, validate_arguments


# Logical operations are a closed catalogue: the model cannot choose URLs or
# forward credentials. The shared Istio template/import routes are excluded.
CATALOG = {
    "resource_inventory": ("GET", "/api/db/res/list", "stored", True),
    "resource_history": ("GET", "/api/db/res/collection", "historical", True),
    "deployment_metrics": ("GET", "/api/prom_query", "metrics", True),
    "namespaces": ("GET", "/api/agent/namespaces", "live", True),
    "pods": ("GET", "/api/get_dpm_pods", "live", True),
    "events": ("GET", "/api/events", "live", True),
    "logs": ("GET", "/api/pod/get_logs", "live", True),
    "previous_logs": ("GET", "/api/pod/get_previous_logs", "live", True),
    "nodes": ("GET", "/api/nodes", "live", True),
    "services": ("GET", "/api/agent/services", "live", True),
    "configmaps": ("GET", "/api/agent/configmaps", "live", True),
    "ingresses": ("GET", "/api/agent/ingresses", "live", True),
    "statefulsets": ("GET", "/api/agent/statefulsets", "live", True),
    "daemonsets": ("GET", "/api/agent/daemonsets", "live", True),
    "jvm_configs": ("GET", "/api/agent/jvm/configs", "live", True),
    "metrics": ("POST", "/api/ai/internal/metrics", "metrics", True),
    "restart_deployment": ("POST", "/api/restart", "live", False),
    "scale_deployment": ("POST", "/api/scale", "live", False),
    "schedule_deployment_restart": ("POST", "/api/cron", "live", False),
    "cron_deployment_restart": ("POST", "/api/cron", "live", False),
    "schedule_deployment_scale": ("POST", "/api/cron", "live", False),
    "cron_deployment_scale": ("POST", "/api/cron", "live", False),
    "update_deployment_image": ("POST", "/api/update-image", "live", False),
    "delete_pod": ("GET", "/api/pod/delete_pod", "live", False),
    "isolate_pod": ("POST", "/api/pod/modify_pod", "live", False),
    "jvm_mem": ("GET", "/api/pod/auto_jvm_mem", "live", False),
    "jvm_dump": ("GET", "/api/pod/auto_dump", "live", False),
    "jvm_jstack": ("GET", "/api/pod/auto_jstack", "live", False),
    "jvm_jfr": ("GET", "/api/pod/auto_jfr", "live", False),
    **EXTRA_CATALOG,
}

DEPLOYMENT_SCHEDULES = {
    "schedule_deployment_restart": ("restart", "once"),
    "cron_deployment_restart": ("restart", "cron"),
    "schedule_deployment_scale": ("scale", "once"),
    "cron_deployment_scale": ("scale", "cron"),
}
DEPLOYMENT_CHANGES = {"restart_deployment", "scale_deployment", *DEPLOYMENT_SCHEDULES}


def validate_deployment_change(operation: str, args: dict):
    if operation not in DEPLOYMENT_CHANGES:
        return
    if any(args.get(key) for key in ("pod", "statefulset", "daemonset")) or str(args.get("resource_type", "deployment")).lower() != "deployment":
        raise AIError("此接口仅操作 Deployment；不能用于 Pod、StatefulSet 或 DaemonSet")
    if not all(isinstance(args.get(key), str) and args[key].strip() for key in ("namespace", "deployment")):
        raise AIError("Deployment 操作需要 namespace 和 deployment")
    if operation == "scale_deployment" or DEPLOYMENT_SCHEDULES.get(operation, (None,))[0] == "scale":
        if type(args.get("replicas")) is not int or args["replicas"] < 0:
            raise AIError("replicas 必须为非负整数")
    if "scheduler" in args and type(args["scheduler"]) is not bool:
        raise AIError("scheduler 必须为布尔值")
    nodes = args.get("node_scheduler", [])
    if not isinstance(nodes, list) or any(not isinstance(node, str) or not node.strip() for node in nodes):
        raise AIError("node_scheduler 必须为节点名称数组")
    if nodes and not args.get("scheduler"):
        raise AIError("指定 node_scheduler 时必须启用 scheduler")
    if operation not in DEPLOYMENT_SCHEDULES:
        return
    _, trigger = DEPLOYMENT_SCHEDULES[operation]
    if trigger == "once":
        raw = args.get("run_at")
        if not isinstance(raw, str):
            raise AIError("定时执行需要 run_at（YYYY-MM-DDTHH:mm，北京时间）")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            raise AIError("run_at 必须为有效的日期时间 YYYY-MM-DDTHH:mm") from None
        if parsed.tzinfo is not None or parsed.second or parsed.microsecond or not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::00)?", raw):
            raise AIError("定时任务按北京时间（Asia/Shanghai）执行，run_at 精确到分钟；不支持时区偏移或秒数")
        # /api/cron puts month/day/hour/minute into a CronJob whose
        # spec.timeZone is Asia/Shanghai, then deletes it after the first run.
        # Its year field is not enforced, so only accept the year of the next
        # occurrence in Beijing time; do not silently turn a requested
        # far-future date into this year's job.
        now = beijing_now().replace(tzinfo=None)
        next_year = None
        for year in range(now.year, now.year + 5):
            try:
                occurrence = parsed.replace(year=year)
            except ValueError:
                continue
            if occurrence > now:
                next_year = year
                break
        if parsed.year != next_year:
            raise AIError("现有单次定时接口只在该月日时分（北京时间）的下一次到达执行，不能指定已过去或更远年份的日期；请调整日期或使用支持该需求的通用 K8S 工具")
        if args.get("cron"):
            raise AIError("定时执行使用 run_at；周期执行请使用 cron_deployment_restart/scale")
    else:
        cron = args.get("cron")
        macros = {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
        if not isinstance(cron, str) or not (cron in macros or len(cron.split()) == 5 and re.fullmatch(r"[\w\s*,/?\-]+", cron, re.ASCII)):
            raise AIError("cron 必须为五段 Cron 表达式，例如 0 2 * * *，或 @daily 等标准宏")
        if args.get("run_at"):
            raise AIError("周期执行使用 cron；单次定时请使用 schedule_deployment_restart/scale")


def deployment_request(operation: str, args: dict, env: str):
    validate_deployment_change(operation, args)
    query = {"env": env}
    if args.get("scheduler"):
        query["scheduler"] = "true"
    item = {"namespace": args["namespace"], "deployment_name": args["deployment"]}
    scheduled = operation in DEPLOYMENT_SCHEDULES
    action_type = DEPLOYMENT_SCHEDULES[operation][0] if scheduled else "scale" if operation == "scale_deployment" else "restart"
    if action_type == "scale":
        item["num"] = args["replicas"]
    service = {"deployment_list": [item], "node_scheduler": copy.deepcopy(args.get("node_scheduler", []))}
    if not scheduled:
        return query, service
    _, trigger = DEPLOYMENT_SCHEDULES[operation]
    when = datetime.fromisoformat(args["run_at"]) if trigger == "once" else None
    body = {"type": action_type, "service": service,
            "time": [when.year, when.month, when.day, when.hour, when.minute] if when else "",
            "cron": args["cron"].strip() if trigger == "cron" else ""}
    return query, body


def tool_catalog() -> list[dict]:
    schemas = {
        "resource_inventory": ([], {"namespace": "string", "deployment": "string"}, "已入库 CPU/内存 requests/limits 与四个 JVM 字节参数；null 表示未采集，update 为采集时间"),
        "resource_history": (["date"], {"date": "YYYY-MM-DD"}, "指定日期的集群每日高峰历史快照"),
        "deployment_metrics": ([], {"namespace": "string"}, "Prometheus/VictoriaMetrics 工作负载资源指标"),
        "pods": (["namespace", "deployment"], {"namespace": "string", "deployment": "string"}, "Deployment 的 Pod 明细；全部或多命名空间 Pod 使用 pod_list"),
        "events": ([], {"namespace": "string"}, "K8S 事件，空命名空间为全部"),
        "logs": (["namespace", "pod"], {"namespace": "string", "pod": "string", "lines": "integer 1..5000"}, "最新 Pod 日志；指定容器用通用 api Pod/log"),
        "previous_logs": (["namespace", "pod"], {"namespace": "string", "pod": "string", "lines": "integer 1..5000"}, "Pod 上一次退出容器的日志"),
        "metrics": (["query"], {"query": "MetricsQL/PromQL string", "start": "RFC3339 or UNIX seconds", "end": "RFC3339 or UNIX seconds", "step": "positive seconds"}, "即时查询；start/end 同时传为范围查询；服务器强制集群标签过滤"),
        "scale_deployment": (["namespace", "deployment", "replicas"], {"namespace": "string", "deployment": "string", "replicas": "nonnegative integer", "scheduler": "boolean optional", "node_scheduler": "string[] optional; requires scheduler=true"}, "Deployment 立即扩缩容；定时一次用 schedule_deployment_scale，周期执行用 cron_deployment_scale；需批准"),
        "restart_deployment": (["namespace", "deployment"], {"namespace": "string", "deployment": "string", "scheduler": "boolean optional", "node_scheduler": "string[] optional"}, "立即滚动重启 Deployment；指定日期执行用 schedule_deployment_restart，每日等重复执行用 cron_deployment_restart；需批准"),
        "update_deployment_image": (["namespace", "deployment", "image_tag"], {"namespace": "string", "deployment": "string", "image_tag": "string"}, "更新镜像标签，沿用 UPDATE_IMAGE 策略，需批准"),
    }
    schemas.update(EXTRA_SCHEMAS)
    for name, (action_type, trigger) in DEPLOYMENT_SCHEDULES.items():
        required = ["namespace", "deployment", "run_at" if trigger == "once" else "cron"]
        arguments = {"namespace": "string", "deployment": "string (Deployment name only)",
                     "run_at" if trigger == "once" else "cron": "YYYY-MM-DDTHH:mm，北京时间（Asia/Shanghai），分钟精度、无时区偏移；仅该月日时分的下一次到达执行，原接口不强制年份" if trigger == "once" else "5-field Cron，按北京时间, e.g. 0 2 * * * = 每天北京时间 02:00；或 @daily 等标准宏"}
        if action_type == "scale":
            required.append("replicas")
            arguments["replicas"] = "nonnegative integer"
        arguments.update(scheduler="boolean optional", node_scheduler="string[] optional; requires scheduler=true")
        schemas[name] = (required, arguments, f"{'指定日期时间执行一次' if trigger == 'once' else '按 Cron 重复执行'}的 Deployment {'重启' if action_type == 'restart' else '扩缩容'}；复用页面 /api/cron 注册任务，禁止用于 Pod/StatefulSet/DaemonSet，无需另写实现 YAML；创建不等于已执行；按北京时间执行（CronJob spec.timeZone=Asia/Shanghai），需批准")
    result = []
    for name, item in CATALOG.items():
        required, arguments, description = schemas.get(name, ([], {"namespace": "string"}, "查询实际 K8S 资源"))
        if name in ("delete_pod", "isolate_pod", "jvm_mem", "jvm_dump", "jvm_jstack", "jvm_jfr"):
            required, arguments, description = ["namespace", "pod"], {"namespace": "string", "pod": "string"}, "Pod 操作或 JVM exec，需批准，可能生成及上传文件"
        result.append({"name": name, "description": description, "read_only": item[3], "freshness": item[2], "required": required, "arguments": arguments})
    return result


def table_rows(result: dict) -> dict:
    if isinstance(result.get("meta"), list) and isinstance(result.get("data"), list):
        names = [x["name"] for x in result["meta"]]
        result = {**result, "data": [dict(zip(names, row)) for row in result["data"]]}
    return result


def guard_scope(scope: dict, operation: str, arguments: dict, read_only: bool):
    """Only the cluster is a hard boundary; other selectors are context."""
    for key in ("env", "k8s"):
        if arguments.get(key) not in (None, scope["env"]):
            raise AIError("工具试图操作其他集群", 403, "scope_violation")
    # Namespace/deployment/Pod are defaults, never authorization boundaries.
    # CLI kubeconfig/context/server override flags are checked by the executor.


@dataclass
class RunContext:
    run: dict
    identity: Identity
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    executors: dict = field(default_factory=dict)
    executor_bindings: dict = field(default_factory=dict)
    executor_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_calls: dict = field(default_factory=dict)
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutorBinding:
    executor: object
    fingerprint: str
    revision: int


class PendingAction(Exception):
    def __init__(self, action):
        self.action = action


class Gateway:
    def __init__(self, store, cipher: CredentialCipher, master_url: str, token: str, http=None):
        self.store, self.cipher = store, cipher
        self.master_url, self.token = master_url.rstrip("/"), token
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(330, connect=10), follow_redirects=False)

    async def master(self, identity, method, path, params=None, body=None, allow_failure=False):
        headers = {"X-Kubedoor-Token": self.token, "X-User-Name": identity.username, "X-User-Permission": identity.permission}
        try:
            response = await self.http.request(method, self.master_url + path, params=params, json=body, headers=headers)
            if allow_failure:
                result = response.json()
                if isinstance(result, dict):
                    if not response.is_success:
                        detail = result.get("error") or result.get("message") or f"HTTP {response.status_code}"
                        return {**result, "success": False, "error": {"code": "API_ERROR", "status": response.status_code, "message": str(redact(detail))}}
                    return result
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPStatusError as exc:
            # Do not log the URL: metrics addresses may contain upstream auth.
            raise AIError(f"KubeDoor 接口返回 HTTP {exc.response.status_code}", exc.response.status_code, "upstream_error") from None
        except (httpx.HTTPError, ValueError):
            raise AIError("KubeDoor 接口暂不可用", 502, "upstream_error") from None
        if isinstance(result, dict) and (result.get("success") is False or result.get("error")):
            raise AIError(str(redact(result.get("error") or result.get("message") or "操作失败")), 502, "operation_failed")
        return result

    async def executor_binding(self, ctx, source, expected_fingerprint=None, expected_revision=None):
        from kubedoor_tools import KubernetesExecutor
        # Parallel investigators share a context. A binding is immutable and
        # remains alive until all calls finish, even if the saved config changes.
        async with ctx.executor_lock:
            row = await self.store.connection(ctx.run["scope"]["env"])
            if not row:
                raise AIError("当前集群没有配置 kubeconfig，可使用 agent 来源", 409, "connection_missing")
            fingerprint = self.connection_fingerprint(row)
            if (expected_fingerprint is not None and fingerprint != expected_fingerprint
                    or expected_revision is not None and row["revision"] != expected_revision):
                raise AIError("批准对应的 kubeconfig 已变更，请重新生成操作", 409, "connection_changed")
            key = (source, fingerprint, row["revision"])
            binding = ctx.executor_bindings.get(key)
            if binding is None:
                configuration = self.cipher.decrypt(row["env"], bytes(row["encrypted_config"]))
                binding = ExecutorBinding(KubernetesExecutor(configuration, row["context"]), fingerprint, row["revision"])
                ctx.executor_bindings[key] = binding
            ctx.executors[source] = binding.executor
            return binding

    async def executor(self, ctx, source, expected_fingerprint=None, expected_revision=None):
        return (await self.executor_binding(ctx, source, expected_fingerprint, expected_revision)).executor

    @staticmethod
    def connection_fingerprint(row):
        return hashlib.sha256(bytes(row["encrypted_config"]) + row["context"].encode()).hexdigest()

    async def choose_source(self, ctx, operation, source):
        if source == "auto":
            if operation in CATALOG:
                return "kubedoor"
            return "direct" if await self.store.connection(ctx.run["scope"]["env"]) else "agent"
        if source not in ("kubedoor", "direct", "agent"):
            raise AIError("source 必须为 auto/kubedoor/direct/agent")
        if source == "kubedoor" and operation not in CATALOG:
            raise AIError("没有这个 KubeDoor 目录工具；请选择 direct 或 agent")
        if source != "kubedoor" and operation not in ("api", "kubectl", "istioctl", "diagnostic", "pod_exec"):
            raise AIError("通用执行器 operation 为 api/kubectl/istioctl/diagnostic/pod_exec")
        return source

    def read_only(self, operation, arguments):
        if operation in CATALOG:
            return CATALOG[operation][3]
        from kubedoor_tools import classify_operation
        return bool(classify_operation(operation, arguments).get("read_only"))

    async def check_cancel(self, ctx):
        if ctx.cancelled.is_set():
            raise asyncio.CancelledError()
        row = await self.store.one("SELECT cancel_requested FROM kubedoor_ai_runs WHERE id=$1::uuid", ctx.run["id"])
        if not row or row["cancel_requested"]:
            ctx.cancelled.set()
            raise asyncio.CancelledError()

    async def prepare(self, ctx, source, operation, arguments, call_id):
        env = ctx.run["scope"]["env"]
        if source == "kubedoor":
            query, body = self.catalog_request(operation, arguments, env)
            return {"success": True, "arguments": copy.deepcopy(arguments), "preview": {"operation": operation, "env": env, "arguments": arguments, "method": CATALOG[operation][0], "path": CATALOG[operation][1], "query": query, "body": body}, "read_only": self.read_only(operation, arguments)}
        if source == "agent":
            return await self.master(ctx.identity, "POST", "/api/ai/internal/agent-execute", {"env": env}, {"operation": operation, "arguments": arguments, "call_id": call_id, "phase": "prepare"}, allow_failure=True)
        binding = await self.executor_binding(ctx, source)
        prepared = await binding.executor.prepare(operation, arguments)
        prepared["credential_fingerprint"] = binding.fingerprint
        prepared["connection_revision"] = binding.revision
        return prepared

    async def invoke(self, ctx: RunContext, operation: str, arguments: dict, source="auto", call_id="", mcp=False):
        if not isinstance(arguments, dict):
            raise AIError("工具 arguments 必须为对象")
        await self.check_cancel(ctx)
        args = copy.deepcopy(arguments)
        source = await self.choose_source(ctx, operation, source)
        read_only = self.read_only(operation, args)
        scope = ctx.run["scope"]
        ns = scope["namespace"] or (scope.get("deployment") or scope.get("pod") or {}).get("namespace")
        if source == "kubedoor" and ns:
            args.setdefault("namespace", ns)
        if source == "kubedoor":
            selected_dep, selected_pod = scope.get("deployment"), scope.get("pod")
            if selected_dep and (operation in DEPLOYMENT_CHANGES or operation in ("pods", "update_deployment_image", "resource_inventory", "resource_config_update", "resource_pod_count", "image_tags", "node_resource_rank", "cci_schedule_profile")):
                args.setdefault("deployment", selected_dep["name"])
                args.setdefault("namespace", selected_dep["namespace"])
            if selected_pod and operation in ("logs", "previous_logs", "delete_pod", "isolate_pod", "jvm_mem", "jvm_dump", "jvm_jstack", "jvm_jfr"):
                args.setdefault("pod", selected_pod["name"])
                args.setdefault("namespace", selected_pod["namespace"])
            if operation in ("pods", "update_deployment_image") and (not args.get("namespace") or not args.get("deployment")):
                raise AIError("该工具需要 namespace 和 deployment")
            if operation in ("logs", "previous_logs", "delete_pod", "isolate_pod", "jvm_mem", "jvm_dump", "jvm_jstack", "jvm_jfr") and (not args.get("namespace") or not args.get("pod")):
                raise AIError("该工具需要 namespace 和 pod")
            validate_deployment_change(operation, args)
            if operation in EXTRA_CATALOG:
                validate_arguments(operation, args)
            if operation == "update_deployment_image" and (not isinstance(args.get("image_tag"), str) or not args["image_tag"]):
                raise AIError("image_tag 不能为空")
        guard_scope(scope, operation, args, read_only)
        if not read_only:
            ctx.identity.require_write()
            unknown = await self.store.one("SELECT id FROM kubedoor_ai_actions WHERE run_id=$1::uuid AND state='unknown' AND read_only=false LIMIT 1", ctx.run["id"])
            if unknown:
                raise AIError("本轮已有写入结果未知，请查询实际状态并在新一轮重新提出操作，不能切换来源盲目重试", 409, "outcome_unknown")
        call_id = call_id or f"{ctx.run['id']}:{uuid.uuid4().hex}"
        action = await self.store.one("SELECT * FROM kubedoor_ai_actions WHERE call_id=$1", call_id)
        if not action:
            prepared = await self.prepare(ctx, source, operation, args, call_id) if not read_only else {"arguments": args, "preview": {"operation": operation, "arguments": args}, "read_only": True}
            if prepared.get("success") is False:
                return prepared
            args = prepared.get("arguments", args)
            guard_scope(scope, operation, args, read_only)
            binding = None
            if source == "direct":
                binding = await self.executor_binding(ctx, source, prepared.get("credential_fingerprint"), prepared.get("connection_revision"))
            preview = redact(prepared.get("preview", {}), ctx.secrets)
            if binding:
                preview["credential_fingerprint"] = binding.fingerprint
            action = await self.store.one("INSERT INTO kubedoor_ai_actions(id,run_id,call_id,operation,source,arguments,preview,read_only,connection_revision,state) VALUES($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(call_id) DO UPDATE SET call_id=EXCLUDED.call_id RETURNING *", str(uuid.uuid4()), ctx.run["id"], call_id, operation, source, args, preview, read_only, binding.revision if binding else None, "ready" if read_only else "pending")
        if action["state"] in ("succeeded", "failed", "unknown", "rejected", "cancelled"):
            return action["result"] or {"success": False, "error": {"code": action["state"], "message": "该操作不会重复执行"}}
        if action["state"] == "pending":
            preview = self.action_preview(action, scope)
            if mcp:
                raise PendingAction(preview)
            decision = interrupt({"kind": "approval", **preview})
            if not isinstance(decision, dict) or decision.get("action_id") != action["id"] or decision.get("decision") != "approve":
                result = {"success": False, "error": {"code": "rejected", "message": "用户拒绝了该操作"}}
                await self.store.execute("UPDATE kubedoor_ai_actions SET state='rejected',result=$2,updated_at=now() WHERE id=$1::uuid", action["id"], result)
                return result
        return await self.dispatch(ctx, action)

    @staticmethod
    def action_preview(action, scope):
        preview = redact({k: v for k, v in action["preview"].items() if k != "credential_fingerprint"})
        return redact({"action_id": action["id"], "name": action["operation"], "tool": action["operation"], "env": scope["env"], "source": action["source"], "arguments": action["arguments"], "preview": preview, "diff": preview.get("diff"), "command": preview.get("command"), "expires_at": action.get("expires_at")})

    async def dispatch(self, ctx, action):
        await self.check_cancel(ctx)
        if not action["read_only"]:
            ctx.identity.require_write()
            valid = await self.store.one("SELECT id FROM kubedoor_ai_actions WHERE id=$1::uuid AND expires_at>now()", action["id"])
            if not valid:
                raise AIError("批准已过期，请重新生成操作", 409, "approval_expired")
        if action["source"] == "direct":
            await self.executor_binding(ctx, "direct", action["preview"].get("credential_fingerprint"), action["connection_revision"])
        # The compare-and-set is the durable dispatch ledger, including MCP.
        dispatched = await self.store.one("UPDATE kubedoor_ai_actions SET state='dispatching',updated_at=now() WHERE id=$1::uuid AND state IN ('ready','pending','approved') RETURNING id", action["id"])
        if not dispatched:
            current = await self.store.one("SELECT * FROM kubedoor_ai_actions WHERE id=$1::uuid", action["id"])
            return current["result"] or {"success": False, "error": {"code": "outcome_unknown", "message": "请求已经提交，不会重复执行；请查询实际状态"}}
        event = {"action_id": action["id"], "name": action["operation"], "tool": action["operation"], "source": action["source"], "arguments": redact(action["arguments"], ctx.secrets), "env": ctx.run["scope"]["env"]}
        await self.store.event(ctx.run["id"], "tool_start", event)
        ctx.active_calls[action["call_id"]] = action
        try:
            await self.check_cancel(ctx)
            result = await self.execute_action(ctx, action)
        except asyncio.CancelledError:
            result = {"success": False, "error": {"code": "cancelled" if action["read_only"] else "outcome_unknown", "message": "任务已停止；已提交的修改需要查询实际状态"}}
            await self.store.execute("UPDATE kubedoor_ai_actions SET state=$2,result=$3,updated_at=now() WHERE id=$1::uuid", action["id"], "cancelled" if action["read_only"] else "unknown", result)
            await self.store.event(ctx.run["id"], "tool_result", {**event, "result": result})
            raise
        except Exception as exc:
            before_dispatch = isinstance(exc, AIError) and action["source"] == "direct" and exc.code in {"connection_changed", "connection_missing"}
            code = exc.code if before_dispatch else "tool_failed" if action["read_only"] else "outcome_unknown"
            result = {"success": False, "error": {"code": code, "message": str(exc) if isinstance(exc, AIError) else "工具执行失败，请检查连接及实际资源状态"}}
        finally:
            ctx.active_calls.pop(action["call_id"], None)
        result = redact(result, ctx.secrets)
        error = result.get("error")
        if isinstance(error, str):
            result["error"] = error = {"code": "tool_failed", "message": error}
        error_code = str(error.get("code", "")).lower() if isinstance(error, dict) else ""
        api_status = error.get("status") if isinstance(error, dict) else None
        # Transport status=0 and HTTP 5xx cannot prove a mutation was rejected.
        # Validation/auth 4xx responses remain a known failure that can be fixed.
        api_unknown = error_code == "api_error" and (not isinstance(api_status, int) or not 400 <= api_status < 500)
        if not action["read_only"] and isinstance(error, dict) and (api_unknown or error_code in {"timeout", "cancelled", "canceled", "execution_timeout", "connection_error", "transport_error", "execution_error", "command_failed", "output_limit", "tool_error"}):
            result["error"] = {"code": "outcome_unknown", "upstream_code": error.get("code"), "upstream_error": error, "message": "写请求已经提交但结果未知或部分执行，请查询实际状态；不会自动重放。"}
        result.setdefault("source", action["source"])
        result.setdefault("observed_at", utc_now())
        result.setdefault("truncated", False)
        state = "succeeded" if result.get("success", True) else "unknown" if result.get("error", {}).get("code") == "outcome_unknown" else "failed"
        await self.store.execute("UPDATE kubedoor_ai_actions SET state=$2,result=$3,updated_at=now() WHERE id=$1::uuid", action["id"], state, result)
        await self.store.event(ctx.run["id"], "tool_result", {**event, "result": result})
        return result

    @staticmethod
    def catalog_request(operation, args, env):
        if operation in EXTRA_CATALOG:
            return build_request(operation, args, env)
        if operation in DEPLOYMENT_CHANGES:
            return deployment_request(operation, args, env)
        query, body = {"env": env}, None
        ns, dep, pod = args.get("namespace", args.get("ns")), args.get("deployment"), args.get("pod")
        if operation == "metrics":
            body = {**args, "env": env}
        elif operation in ("resource_inventory", "resource_history"):
            query.update({k: v for k, v in args.items() if k in ("namespace", "deployment", "date")})
            if operation == "resource_history" and not args.get("date"):
                raise AIError("resource_history 需要 date（YYYY-MM-DD），对应每日高峰期快照")
        elif operation == "deployment_metrics":
            query.update({"ns": ns or ""})
        elif operation == "pods":
            if not ns or not dep:
                raise AIError("pods 目录工具需要 namespace 和 deployment；全部 Pod 请使用 pod_list")
            query.update({"namespace": ns, "deployment": dep})
        elif operation in ("logs", "previous_logs"):
            query.update({"ns": ns, "pod": pod, "lines": min(5000, max(1, int(args.get("lines", 100))))})
            if args.get("container"):
                raise AIError("目录日志接口不支持指定 container；请用 api Pod/log 工具")
        elif operation in ("delete_pod", "isolate_pod", "jvm_mem", "jvm_dump", "jvm_jstack", "jvm_jfr"):
            query.update({"ns": ns, "pod_name": pod})
        elif operation == "update_deployment_image":
            body = {"namespace": ns, "deployment": dep, "image_tag": args["image_tag"]}
        elif operation not in ("nodes", "namespaces") and ns:
            query["namespace"] = ns
        return query, body

    async def execute_action(self, ctx, action):
        source, operation, args = action["source"], action["operation"], action["arguments"]
        env = ctx.run["scope"]["env"]
        if source == "direct":
            return await (await self.executor(ctx, source, action.get("preview", {}).get("credential_fingerprint"), action.get("connection_revision"))).execute(operation, args, call_id=action["call_id"], timeout=120)
        if source == "agent":
            return await self.master(ctx.identity, "POST", "/api/ai/internal/agent-execute", {"env": env}, {"operation": operation, "arguments": args, "call_id": action["call_id"], "phase": "execute", "approved": not action["read_only"], "timeout": 120}, allow_failure=True)
        method, path, freshness, _ = CATALOG[operation]
        query, body = self.catalog_request(operation, args, env)
        result = await self.master(ctx.identity, method, path, query, body, allow_failure=True)
        if not isinstance(result, dict):
            result = {"data": result}
        if operation == "resource_content" and isinstance(result.get("data"), str):
            result["data"] = redact({"yaml_content": result["data"]}, ctx.secrets)["yaml_content"]
        if result.get("error_list"):
            result = {**result, "success": False, "error": {"code": "partial_failure", "message": "KubeDoor 操作返回失败项目，请核验实际状态，不应重复提交整批操作。"}}
        elif result.get("error"):
            result["success"] = False
        if operation in DEPLOYMENT_SCHEDULES and result.get("success", True) and not result.get("error"):
            action_type, trigger = DEPLOYMENT_SCHEDULES[operation]
            result["schedule"] = {"resource_type": "Deployment", "namespace": args["namespace"], "deployment": args["deployment"],
                                  "task_namespace": "kubedoor", "task_name": f"{action_type}-{trigger}-{args['deployment']}",
                                  "kind": trigger, "run_at": args.get("run_at"), "cron": args.get("cron"),
                                  "time_zone": "Asia/Shanghai", "registered": True, "executed": False}
        result = table_rows(result)
        return {**result, "success": result.get("success", True), "freshness": freshness, "observed_at": utc_now(), "source": "kubedoor", "truncated": False}

    async def cancel(self, ctx):
        ctx.cancelled.set()
        tasks = []
        for call_id, action in list(ctx.active_calls.items()):
            if action["source"] == "agent":
                tasks.append(self.master(ctx.identity, "POST", "/api/ai/internal/agent-cancel", {"env": ctx.run["scope"]["env"]}, {"call_id": call_id}))
            elif action["source"] == "direct":
                key = ("direct", action.get("preview", {}).get("credential_fingerprint"), action.get("connection_revision"))
                binding = ctx.executor_bindings.get(key)
                executor = binding.executor if binding else ctx.executors.get("direct")
                if executor:
                    tasks.append(executor.cancel(call_id))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_context(self, ctx):
        executors = {id(binding.executor): binding.executor for binding in ctx.executor_bindings.values()}
        executors.update({id(executor): executor for executor in ctx.executors.values()})
        await asyncio.gather(*(e.close() for e in executors.values()), return_exceptions=True)
        ctx.executors.clear()
        ctx.executor_bindings.clear()

    async def close(self):
        await self.http.aclose()
