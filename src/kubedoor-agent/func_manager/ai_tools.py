"""Run shared AI tools only when requested over the master WebSocket."""

import asyncio
import json
from collections import OrderedDict

from kubedoor_tools import KubernetesExecutor, classify_operation


def _encode_response(request_id, response):
    """工具结果可能有几十 MB,序列化在线程里做(调用方负责),超过 8 MiB 只回错误摘要"""
    if len(json.dumps(response).encode()) > 8 * 1024 * 1024:
        error = response.get("error")
        if isinstance(error, dict):
            error = {"code": error.get("code", "OUTPUT_LIMIT"), "message": str(error.get("message", ""))[:1024]}
        elif error:
            error = {"code": "OUTPUT_LIMIT", "message": str(error)[:1024]}
        response = {"success": response.get("success", False), "truncated": True,
                    "data": {"omitted": True, "reason": "工具结果超过 8 MiB，请减少日志行数或通过 limit/continue 分页查询"},
                    "error": error}
    return json.dumps({"type": "response", "request_id": request_id, "ai": True, "response": response})


class AiToolHandler:
    def __init__(self):
        self.executor = KubernetesExecutor()
        self.tasks = {}
        self.phases = {}
        self.owners = {}
        self.completed = OrderedDict()
        self.completed_sizes = {}
        self.completed_bytes = 0

    async def _send(self, ws, request_id, response):
        if ws.closed:
            return
        message = await asyncio.to_thread(_encode_response, request_id, response)
        if not ws.closed:
            await ws.send_str(message)

    def _remember(self, call_id, response):
        size = len(json.dumps(response).encode())
        if size > 256 * 1024:
            response = {"success": response.get("success", False), "truncated": True,
                        "error": response.get("error"), "message": "该请求已经执行；缓存省略了较大的结果，请重新查询实际资源状态"}
            size = len(json.dumps(response).encode())
        self.completed_bytes -= self.completed_sizes.get(call_id, 0)
        self.completed[call_id] = response
        self.completed.move_to_end(call_id)
        self.completed_sizes[call_id] = size
        self.completed_bytes += size
        while len(self.completed) > 2048 or self.completed_bytes > 8 * 1024 * 1024:
            expired, _ = self.completed.popitem(last=False)
            self.completed_bytes -= self.completed_sizes.pop(expired)
            self.owners.pop(expired, None)

    async def handle(self, ws, packet):
        request_id = packet["request_id"]
        username, permission = packet.get("username"), packet.get("permission")
        call_id = packet.get("call_id", "")
        phase = packet.get("phase", "execute")
        if not username or permission not in ("read", "rw") or not isinstance(call_id, str) or not call_id or len(call_id) > 160 or phase not in ("prepare", "execute", "cancel"):
            await self._send(ws, request_id, {"success": False, "error": {"code": "FORBIDDEN", "message": "缺少可信工具调用身份"}})
            return
        if phase == "cancel":
            if call_id in self.owners and self.owners[call_id] != username:
                await self._send(ws, request_id, {"success": False, "error": {"code": "FORBIDDEN", "message": "不能停止其他用户的工具"}})
                return
            task = self.tasks.get(call_id)
            if task:
                task.cancel()
            await self.executor.cancel(call_id)
            await self._send(ws, request_id, {"success": True, "data": {"cancelled": bool(task)}})
            return
        operation, arguments = packet.get("operation"), packet.get("arguments", {})
        try:
            policy = classify_operation(operation, arguments)
            if not policy["read_only"] and (permission != "rw" or (phase == "execute" and packet.get("approved") is not True)):
                await self._send(ws, request_id, {"success": False, "error": {"code": "APPROVAL_REQUIRED", "message": "修改或命令执行需要读写权限及准确操作确认"}})
                return
            if call_id and call_id in self.owners and self.owners[call_id] != username:
                raise PermissionError("工具调用编号已被其他用户使用")
            self.owners[call_id] = username
            if phase == "execute" and call_id in self.completed:
                await self._send(ws, request_id, self.completed[call_id])
                return
            if phase == "execute" and self.phases.get(call_id) == "prepare":
                await self._send(ws, request_id, {"success": False, "error": {"code": "BUSY", "message": "操作仍在准备，尚未执行，请等待预览返回"}})
                return
            if phase == "execute" and call_id in self.tasks:
                response = await asyncio.shield(self.tasks[call_id])
            else:
                async def run():
                    if phase == "prepare":
                        return await self.executor.prepare(operation, arguments, timeout=packet.get("timeout", 120))
                    return await self.executor.execute(operation, arguments, call_id=call_id, timeout=packet.get("timeout", 120))

                task = asyncio.create_task(run())
                self.tasks[call_id] = task
                self.phases[call_id] = phase
                try:
                    response = await task
                    if phase == "execute" and not policy["read_only"]:
                        self._remember(call_id, response)
                finally:
                    if self.tasks.get(call_id) is task:
                        self.tasks.pop(call_id, None)
                        self.phases.pop(call_id, None)
            await self._send(ws, request_id, response)
        except asyncio.CancelledError:
            response = {"success": False, "error": {"code": "CANCELLED", "message": "已停止等待；已提交的修改请检查实际状态"}}
            if phase == "execute" and not policy["read_only"]:
                self._remember(call_id, response)
            await self._send(ws, request_id, response)
        except Exception:
            # Never log arguments or credentials from a tool result.
            await self._send(ws, request_id, {"success": False, "error": {"code": "TOOL_ERROR", "message": "工具执行失败，请检查参数及集群访问权限"}})
        finally:
            if call_id not in self.tasks and call_id not in self.completed:
                self.owners.pop(call_id, None)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.executor.close()
