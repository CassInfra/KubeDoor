"""权限与审批：按 master「权限与审批」页面的操作权限表判定 AI 工具调用（网页 AI、企微 AI、外部 MCP 一致）。

- 先算出调用涉及的操作类型（ops）：目录工具查 TOOL_OPS，通用工具（api/kubectl/istioctl/diagnostic/pod_exec）
  由 kubedoor-tools 按命令清单分类、识别 CoreDNS / Istio 目标。
- 纯查询（ops 为空）不进表、永不审批；有 ops 时按账号在该集群的有效权限查表，任一不允许就拒绝（403，不建动作）。
- 都允许时，任一 op 的审批列为真、或是高危命令（固定审批），就要提问人批准，否则直接执行。
- 策略从 master 的 /api/ai/internal/permission-policy 获取，缓存 5 秒；取不到时有 ops 的调用一律拒绝，查询不受影响。
"""
from __future__ import annotations

import asyncio
import logging
import time

LOGGER = logging.getLogger(__name__)

POLICY_TTL = 5.0
GENERIC_OPERATIONS = frozenset({"api", "kubectl", "istioctl", "diagnostic", "pod_exec"})
PERMISSIONS = ("read", "rw", "admin")
PERMISSION_LABELS = {"none": "无", "read": "只读", "rw": "读写", "admin": "管理员"}

# 高危命令固定要批准，权限表里取消也不行
FIXED_APPROVAL = frozenset({"pod.exec.danger"})
# 审批列为「—」的行（不是 AI 操作，或是查看）：一律不审批
NO_APPROVAL = frozenset({"pod.shell", "node.shell", "resource.secret", "network.tcp", "alert.operate", "alert.silence"})

# 拒绝时告诉用户不能执行什么；名称同 master auth/permissions.py 的 OPERATIONS
OP_NAMES = {
    "pod.shell": "网页 Pod 终端",
    "node.shell": "网页节点终端",
    "pod.exec.view": "AI 在 Pod 内执行查看命令",
    "pod.exec.other": "AI 在 Pod 内执行其他命令",
    "pod.exec.danger": "AI 在 Pod 内执行高危命令",
    "workload.restart": "重启",
    "workload.scale": "扩缩容",
    "workload.schedule": "定时 / 周期重启、扩缩容",
    "workload.image": "更新镜像",
    "pod.delete": "删除 Pod",
    "pod.isolate": "隔离 Pod",
    "pod.jvm": "JVM 诊断",
    "resource.apply": "新建 / 编辑资源 YAML",
    "resource.delete": "删除资源",
    "resource.secret": "查看 Secret 内容",
    "ai.k8s_write": "AI 通用修改",
    "ai.diagnostic": "AI 诊断工具",
    "node.cordon": "禁止 / 允许调度",
    "balance.run": "节点均衡",
    "control.edit": "资源管控",
    "network.coredns": "CoreDNS 修改",
    "network.tcp": "端口测试",
    "network.istio": "Istio 修改",
    "alert.operate": "告警处理状态",
    "alert.silence": "告警屏蔽",
}

# 目录工具里的修改 → 操作类型；查询类工具不在这里（没有操作类型）。
# 测试保证 gateway.CATALOG（含 catalogue.EXTRA_CATALOG）里每个非只读工具都有映射
TOOL_OPS = {
    "restart_deployment": ("workload.restart",),
    "restart_statefulset": ("workload.restart",),
    "restart_daemonset": ("workload.restart",),
    "scale_deployment": ("workload.scale",),
    "scale_statefulset": ("workload.scale",),
    "resource_pod_count": ("workload.scale",),
    "schedule_deployment_restart": ("workload.schedule",),
    "cron_deployment_restart": ("workload.schedule",),
    "schedule_deployment_scale": ("workload.schedule",),
    "cron_deployment_scale": ("workload.schedule",),
    "update_deployment_image": ("workload.image",),
    "delete_pod": ("pod.delete",),
    "delete_pods": ("pod.delete",),
    "isolate_pod": ("pod.isolate",),
    "jvm_mem": ("pod.jvm",),
    "jvm_dump": ("pod.jvm",),
    "jvm_jstack": ("pod.jvm",),
    "jvm_jfr": ("pod.jvm",),
    "cordon_nodes": ("node.cordon",),
    "uncordon_nodes": ("node.cordon",),
    "resource_apply": ("resource.apply",),
    "resource_delete": ("resource.delete",),
    "load_balance_analyze": ("balance.run",),
    "load_balance_execute": ("balance.run",),
    "load_balance_cleanup": ("balance.run",),
    "resource_config_update": ("control.edit",),
    "resource_control_add": ("control.edit",),
    "update_deployment_resources": ("control.edit",),
    "update_deployment_jvm": ("control.edit",),
}
# 这两个再按目标追加 network.coredns / network.istio
TARGETED_TOOLS = frozenset({"resource_apply", "resource_delete"})
# 通用工具的分类没给出 ops（参数异常等）时，修改按这一类处理，不能因此绕过权限表
FALLBACK_OPS = {"api": "ai.k8s_write", "kubectl": "ai.k8s_write", "istioctl": "ai.k8s_write",
                "diagnostic": "ai.diagnostic", "pod_exec": "pod.exec.danger"}
TARGET_OPS = (("coredns", "network.coredns"), ("istio", "network.istio"))
UNREGISTERED = "unregistered"


def normalize_policy(raw) -> dict | None:
    """校验 master 返回的策略，再强制一次固定的审批值；格式不对返回 None（按获取失败处理）"""
    if not isinstance(raw, dict) or not isinstance(raw.get("ops"), dict) or not isinstance(raw.get("commands"), dict):
        return None
    ops = {}
    for key, rule in raw["ops"].items():
        if not isinstance(key, str) or not isinstance(rule, dict):
            return None
        item = {perm: rule.get(perm) is True for perm in PERMISSIONS}
        item["approval"] = key not in NO_APPROVAL and (key in FIXED_APPROVAL or rule.get("approval") is True)
        ops[key] = item
    commands = {}
    for kind in ("view", "danger"):
        items = raw["commands"].get(kind)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            return None
        commands[kind] = list(items)
    version = raw.get("version")
    return {"version": "" if version is None else str(version), "ops": ops, "commands": commands}


class PolicyCache:
    """master 权限策略的进程内缓存：5 秒内复用；获取失败不缓存，返回 None"""

    def __init__(self, ttl: float = POLICY_TTL, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._policy, self._at = None, 0.0
        self._lock = asyncio.Lock()

    def _fresh(self):
        return self._policy is not None and self.clock() - self._at < self.ttl

    async def get(self, fetch):
        """fetch 是无参的协程函数，返回 master 的原始响应"""
        if self._fresh():
            return self._policy
        async with self._lock:
            if self._fresh():   # 等锁期间别的调用已经取到了
                return self._policy
            try:
                policy = normalize_policy(await fetch())
            except Exception as exc:
                LOGGER.warning("获取权限策略失败: %s", type(exc).__name__)
                return None
            if policy is None:
                LOGGER.warning("master 返回的权限策略格式无效")
                return None
            self._policy, self._at = policy, self.clock()
            return policy

    def invalidate(self):
        self._policy = None


def _unique(ops) -> list[str]:
    return list(dict.fromkeys(ops))


def target_ops(operation: str, args: dict) -> list[str]:
    """resource_apply / resource_delete 涉及 kube-system/coredns 或 Istio 资源时追加的操作类型，判断不了按涉及处理"""
    from kubedoor_tools.policy import identify_targets
    try:
        if operation == "resource_apply":
            targets = identify_targets(yaml_content=str(args.get("yaml_content") or ""))
        else:
            targets = identify_targets(resource_type=str(args.get("resource_type") or "").lower(),
                                       name=args.get("resource_name"), namespace=args.get("namespace"))
    except Exception:
        targets = {"coredns", "istio"}
    return [op for target, op in TARGET_OPS if target in targets]


def catalog_ops(operation: str, args: dict) -> tuple[list[str], bool]:
    """目录工具：返回 (ops, read_only)。没登记操作类型的修改返回 UNREGISTERED，查表时一律拒绝"""
    if operation in TOOL_OPS:
        ops = list(TOOL_OPS[operation])
        if operation in TARGETED_TOOLS:
            ops += target_ops(operation, args)
        return _unique(ops), False
    if operation == "resource_content" and str(args.get("resource_type") or "").lower() in ("secret", "secrets"):
        return ["resource.secret"], True
    from .gateway import CATALOG   # 目录定义在 gateway 里，运行时再取，避免循环导入
    entry = CATALOG.get(operation)
    if entry is not None and entry[3]:
        return [], True
    return [UNREGISTERED], False


def operation_ops(operation: str, args: dict, commands: dict | None = None) -> tuple[list[str], bool]:
    """返回 (ops, read_only)。read_only 是操作本身不改东西（决定去重、结果未知、能否重新生成、是否 prepare）"""
    if operation not in GENERIC_OPERATIONS:
        return catalog_ops(operation, args)
    from kubedoor_tools import classify_operation
    classified = classify_operation(operation, args, commands)
    read_only = bool(classified.get("read_only"))
    ops = _unique(op for op in classified.get("ops") or [] if isinstance(op, str))
    if not read_only and not ops:
        ops = [FALLBACK_OPS[operation]]
    return ops, read_only


def needs_policy(operation: str, args: dict) -> bool:
    """查询类目录工具不查表，不必等 master；通用工具的命令分类要用策略里的命令清单"""
    return operation in GENERIC_OPERATIONS or bool(catalog_ops(operation, args)[0])


def check(identity, env: str, ops, policy: dict | None) -> str | None:
    """ops 是否全部允许；不允许时返回说明（直接告诉模型和用户），允许返回 None"""
    perm = identity.permission_for(env)
    if perm not in PERMISSIONS:
        return f"当前账号没有集群 {env} 的访问权限"
    if not ops:
        return None
    if policy is None:
        return "暂时无法从 KubeDoor 获取「权限与审批」配置，不能执行修改或 Pod 内命令（查询不受影响），请稍后重试"
    for op in ops:
        rule = policy["ops"].get(op)
        name = OP_NAMES.get(op) or ("未登记操作类型的修改" if op == UNREGISTERED else op)
        if rule is None:
            return f"操作类型「{name}」不在「权限与审批」的权限表里，不能执行；请确认 KubeDoor 各组件已升级到同一版本"
        if not rule.get(perm):
            if not any(rule.get(level) for level in PERMISSIONS):
                return f"「{name}」已被管理员在「权限与审批」里关闭，任何账号都不能执行"
            return f"当前账号在集群 {env} 是「{PERMISSION_LABELS[perm]}」权限，不能执行「{name}」"
    return None


def evaluate(identity, env: str, operation: str, args: dict, policy: dict | None) -> dict:
    """权限先于审批。返回 ops、read_only、approval（要提问人批准）、approval_fixed（高危，固定审批）、
    denied（没有权限，不建动作）和 message（拒绝原因）"""
    ops, read_only = operation_ops(operation, args, policy["commands"] if policy else None)
    message = check(identity, env, ops, policy)
    result = {"ops": ops, "read_only": read_only, "approval": False, "approval_fixed": False,
              "denied": message is not None, "message": message}
    if message is None and ops:
        rules = policy["ops"]
        result["approval_fixed"] = any(op in FIXED_APPROVAL for op in ops)
        result["approval"] = result["approval_fixed"] or any(rules[op]["approval"] and op not in NO_APPROVAL for op in ops)
    return result
