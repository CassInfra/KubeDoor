"""AI 用的 Deployment 管控状态:判断规则和准入热路径 get_deploy_admis_async 一致,另加 webhook / 命名空间标签核实。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from func_manager import deployment_control as dc  # noqa: E402


def agent_row(admission=True, namespaces=("team",), free=False, scheduler=False):
    return {"admission": admission, "admission_namespace": json.dumps(list(namespaces)),
            "nms_not_confirm": free, "scheduler": scheduler}


def control_row(**values):
    row = {"pod_count": 3, "pod_count_ai": -1, "pod_count_manual": -1, "request_cpu_m": 200, "request_mem_mb": 512,
           "limit_cpu_m": 2000, "limit_mem_mb": 4096, "max_unavailable": None, "max_surge": None,
           "jvm_xms_bytes": None, "jvm_xmx_bytes": 1024 ** 3, "jvm_xss_bytes": None, "jvm_max_metaspace_bytes": None,
           "update": None}
    row.update(values)
    return row


@pytest.mark.parametrize("agent, control, verify, state", [
    (None, control_row(), {}, "cluster_off"),
    (agent_row(admission=False), control_row(), {}, "cluster_off"),
    (agent_row(namespaces=("other",)), control_row(), {}, "namespace_off"),
    (agent_row(), control_row(), {}, "controlled"),
    (agent_row(), control_row(), {"webhook_present": True, "namespace_ignored": False}, "controlled"),
    # 数据库说开着,集群里的 webhook 被删了:直接改会生效,不能再按受管控拦
    (agent_row(), control_row(), {"webhook_present": False}, "webhook_missing"),
    (agent_row(), control_row(), {"namespace_ignored": True}, "namespace_ignored"),
    (agent_row(free=True), None, {}, "unregistered"),
    (agent_row(free=False), None, {}, "unregistered_blocked"),
])
def test_classify_matches_the_admission_hot_path(agent, control, verify, state):
    assert dc.classify(agent, control, "team", **verify) == state


def test_effective_values_follow_the_webhook_rules():
    # 副本优先级:人工 > AI 推荐 > 当日 Pod;requests 过小时按 webhook 规整;未配置(<=0)的项 webhook 不改
    assert dc.effective_values(control_row())["replicas"] == 3
    assert dc.effective_values(control_row(pod_count_ai=4))["replicas"] == 4
    assert dc.effective_values(control_row(pod_count_ai=4, pod_count_manual=0))["replicas"] == 0
    values = dc.effective_values(control_row(request_cpu_m=5, request_mem_mb=0, limit_cpu_m=-1))
    assert (values["request_cpu_m"], values["request_mem_mb"], values["limit_cpu_m"], values["limit_mem_mb"]) == (10, 1, None, 4096)
    assert dc.effective_values(control_row(request_cpu_m=-1))["request_cpu_m"] is None
    assert dc.effective_values(control_row())["jvm_xmx_bytes"] == 1024 ** 3


@pytest.mark.parametrize("namespace, deployment, ok", [
    ("team", "order-api", True), ("team", "v1.2-api", True), ("Team", "x", False),
    ("team/../x", "x", False), ("team", "", False), ("a" * 64, "x", False), ("team", None, False),
])
def test_target_names_are_validated_before_building_api_paths(namespace, deployment, ok):
    assert dc.valid_target(namespace, deployment) is ok


def rpc_returning(responses):
    calls = []

    async def rpc(clients, env, payload, timeout):
        calls.append(payload)
        return responses[payload["arguments"]["path"]]
    return rpc, calls


@pytest.mark.asyncio
async def test_verify_reads_webhook_and_namespace_label_read_only():
    rpc, calls = rpc_returning({
        dc.WEBHOOK_PATH: {"success": False, "error": {"code": "NOT_FOUND"}},
        "/api/v1/namespaces/team": {"success": True, "data": {"metadata": {"labels": {"kubedoor-ignore": "true"}}}},
    })
    clients = {"prod": {"online": True, "ai_tools": True}}
    assert await dc.verify_admission(clients, rpc, "prod", "team", "alice") == (False, True)
    assert {c["permission"] for c in calls} == {"read"} and not any(c["approved"] for c in calls)
    assert all(c["arguments"]["method"] == "GET" for c in calls)
    # 旧版 agent / 离线:不核实,按数据库判断
    assert await dc.verify_admission({"prod": {"online": True, "ai_tools": False}}, rpc, "prod", "team", "alice") == (None, None)


@pytest.mark.asyncio
async def test_verify_failure_falls_back_to_the_database():
    async def broken(*_args, **_kwargs):
        raise TimeoutError
    assert await dc.verify_admission({"prod": {"online": True, "ai_tools": True}}, broken, "prod", "team", "u") == (None, None)


@pytest.mark.asyncio
async def test_status_skips_verification_when_the_database_already_says_uncontrolled():
    rpc, calls = rpc_returning({})

    async def fetchrow(sql, *args):
        return agent_row(namespaces=("other",)) if "k8s_agent_status" in sql else control_row()

    data = await dc.deployment_control({"prod": {"online": True, "ai_tools": True}}, rpc, fetchrow, "prod", "team", "api", "u")
    assert (data["state"], data["controlled"], data["registered"]) == ("namespace_off", False, True)
    assert data["effective"]["replicas"] == 3 and calls == []
