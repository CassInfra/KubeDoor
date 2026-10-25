"""AI 用的 Deployment 管控状态:花折准入控制会不会把副本数、资源、JVM 改写成管控表的值。

判断规则和准入热路径 utils.get_deploy_admis_async 一致:集群开启准入控制 + 命名空间在管控列表 +
管控表里有该服务。另外 agent 在线时实时核实两件数据库看不到的事:准入 webhook 是否真的存在
(紧急情况下可能被手动删掉)、命名空间是否带 kubedoor-ignore 标签(webhook 不处理这类命名空间)。
"""

import asyncio
import json
import re
import uuid

WEBHOOK_PATH = "/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations/kubedoor-admis-configuration"
IGNORE_LABEL = "kubedoor-ignore"
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?\Z")
JVM_FIELDS = ("jvm_xms_bytes", "jvm_xmx_bytes", "jvm_xss_bytes", "jvm_max_metaspace_bytes")
CONTROL_COLUMNS = ("pod_count", "pod_count_ai", "pod_count_manual", "request_cpu_m", "request_mem_mb",
                   "limit_cpu_m", "limit_mem_mb", "max_unavailable", "max_surge", *JVM_FIELDS, "update")
# "update" 是保留字,列名统一加引号
_SELECT_CONTROL = ("SELECT " + ", ".join(f'"{column}"' for column in CONTROL_COLUMNS)
                   + " FROM k8s_res_control WHERE env=$1 AND namespace=$2 AND deployment=$3")
MESSAGES = {
    "controlled": "受花折资源管控：准入控制会把副本数、requests/limits、JVM 参数和滚动策略改写成管控表的值",
    "cluster_off": "集群未开启准入控制，不受管控",
    "namespace_off": "命名空间不在准入控制的管控列表中，不受管控",
    "namespace_ignored": f"命名空间带 {IGNORE_LABEL} 标签，准入控制不处理，不受管控",
    "webhook_missing": "master 记录已开启准入控制，但集群里没有准入 webhook（可能被手动删除），实际不受管控；"
                       "请在「Agent管理」重新开关一次准入控制，让状态同步",
    "unregistered": "管控表里没有该服务，集群开启了新服务免确认：直接放行，不受管控",
    "unregistered_blocked": "管控表里没有该服务，且未开启新服务免确认：这个 Deployment 的创建和修改都会被准入控制拒绝，"
                            "需要先登记到资源管控",
}


def valid_target(namespace, deployment):
    return isinstance(namespace, str) and bool(_NAMESPACE.match(namespace)) \
        and isinstance(deployment, str) and bool(_NAME.match(deployment))


def admission_namespaces(raw):
    """k8s_agent_status.admission_namespace 是前端写入的 JSON 数组字符串"""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def classify(agent, control, namespace, webhook_present=None, namespace_ignored=None):
    """返回 MESSAGES 里的状态;webhook_present / namespace_ignored 为 None 表示没能核实,按数据库判断"""
    if not agent or not agent["admission"]:
        return "cluster_off"
    if namespace not in admission_namespaces(agent["admission_namespace"]):
        return "namespace_off"
    if namespace_ignored:
        return "namespace_ignored"
    if webhook_present is False:
        return "webhook_missing"
    if control is None:
        return "unregistered" if agent["nms_not_confirm"] else "unregistered_blocked"
    return "controlled"


def effective_values(control):
    """准入 webhook 实际写入的值,取值规则同 agent admis_mutate;None 表示 webhook 不改这一项"""
    manual, ai, count = control["pod_count_manual"], control["pod_count_ai"], control["pod_count"]
    request_cpu_m = 10 if 0 <= control["request_cpu_m"] < 10 else control["request_cpu_m"]
    request_mem_mb = 1 if control["request_mem_mb"] == 0 else control["request_mem_mb"]
    return {
        "replicas": manual if manual >= 0 else ai if ai >= 0 else count,
        "request_cpu_m": request_cpu_m if request_cpu_m > 0 else None,
        "request_mem_mb": request_mem_mb if request_mem_mb > 0 else None,
        "limit_cpu_m": control["limit_cpu_m"] if control["limit_cpu_m"] > 0 else None,
        "limit_mem_mb": control["limit_mem_mb"] if control["limit_mem_mb"] > 0 else None,
        **{field: control[field] for field in JVM_FIELDS},
    }


def _plain(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


async def verify_admission(clients, rpc, env, namespace, username):
    """经 agent 只读核实 webhook 和命名空间标签;agent 离线、版本不支持或超时都返回 (None, None)"""
    connection = clients.get(env) or {}
    if not connection.get("online") or not connection.get("ai_tools"):
        return None, None

    async def get(path):
        return await rpc(clients, env, {
            "phase": "execute", "operation": "api", "arguments": {"method": "GET", "path": path},
            "call_id": f"control:{uuid.uuid4().hex}", "approved": False, "timeout": 8,
            "username": username, "permission": "read",
        }, timeout=10)

    try:
        webhook, ns = await asyncio.gather(get(WEBHOOK_PATH), get(f"/api/v1/namespaces/{namespace}"))
    except Exception:
        return None, None
    webhook_present = None
    if isinstance(webhook, dict):
        if webhook.get("success"):
            webhook_present = True
        elif (webhook.get("error") or {}).get("code") == "NOT_FOUND":
            webhook_present = False
    namespace_ignored = None
    if isinstance(ns, dict) and ns.get("success") and isinstance(ns.get("data"), dict):
        labels = (ns["data"].get("metadata") or {}).get("labels") or {}
        namespace_ignored = IGNORE_LABEL in labels
    return webhook_present, namespace_ignored


async def deployment_control(clients, rpc, fetchrow, env, namespace, deployment, username):
    agent = await fetchrow(
        "SELECT admission, admission_namespace, nms_not_confirm, scheduler FROM k8s_agent_status WHERE env=$1", env)
    control = await fetchrow(_SELECT_CONTROL, env, namespace, deployment)
    state = classify(agent, control, namespace)
    webhook_present = namespace_ignored = None
    if state not in ("cluster_off", "namespace_off"):
        webhook_present, namespace_ignored = await verify_admission(clients, rpc, env, namespace, username)
        state = classify(agent, control, namespace, webhook_present, namespace_ignored)
    values = {column: _plain(control[column]) for column in CONTROL_COLUMNS} if control else None
    return {
        "env": env, "namespace": namespace, "deployment": deployment,
        "state": state, "controlled": state == "controlled", "blocked": state == "unregistered_blocked",
        "message": MESSAGES[state],
        "admission": {
            "enabled": bool(agent and agent["admission"]),
            "namespace_listed": bool(agent) and namespace in admission_namespaces(agent["admission_namespace"]),
            "new_service_free": bool(agent and agent["nms_not_confirm"]),
            "scheduler": bool(agent and agent["scheduler"]),
            "webhook_present": webhook_present, "namespace_ignored": namespace_ignored,
        },
        "registered": control is not None,
        "control": values,
        "effective": effective_values(control) if control else None,
    }
