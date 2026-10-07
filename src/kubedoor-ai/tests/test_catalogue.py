"""Contracts from the existing web/master/agent routes, without live clusters."""
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from fakes import FakeStore
from kubedoor_ai.catalogue import EXTRA_CATALOG, EXTRA_SCHEMAS, build_request, validate_arguments
from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.gateway import CATALOG, Gateway, PendingAction, RunContext


ENV = "cluster-a"
YAML = "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: backup\n  namespace: ops\nspec:\n  schedule: '0 2 * * *'\n"
CONTRACTS = [
    ("pod_list", {"namespaces": ["ops", "apps"], "node_name": "n1"}, "GET", "/api/agent/pods", {"namespaces": "ops,apps", "node_name": "n1"}, None),
    ("delete_pods", {"pods": [{"namespace": "ops", "pod": "app-1", "path": "/admin"}]}, "DELETE", "/api/pod/delete_pods", {}, {"pods": [{"ns": "ops", "pod_name": "app-1"}]}),
    ("node_list", {"simple": True}, "GET", "/api/nodes/list", {"simple": "true"}, None),
    ("cordon_nodes", {"node_names": ["n1"]}, "POST", "/api/nodes/cordon", {}, {"node_names": ["n1"]}),
    ("uncordon_nodes", {"node_names": ["n1"]}, "POST", "/api/nodes/uncordon", {}, {"node_names": ["n1"]}),
    ("service_endpoints", {"namespace": "ops", "service_name": "app"}, "GET", "/api/agent/service/endpoints", {"namespace": "ops", "service_name": "app"}, None),
    ("service_first_port", {"namespace": "ops", "service_name": "app"}, "GET", "/api/agent/service/first-port", {"namespace": "ops", "service_name": "app"}, None),
    ("ingress_rules", {"namespace": "ops", "ingress_name": "app"}, "GET", "/api/agent/ingress/rules", {"namespace": "ops", "ingress_name": "app"}, None),
    ("resource_content", {"namespace": "ops", "resource_type": "CronJob", "resource_name": "backup"}, "GET", "/api/agent/res/content", {"namespace": "ops", "resource_type": "cronjob", "resource_name": "backup"}, None),
    ("resource_apply", {"yaml_content": YAML, "method": "apply"}, "POST", "/api/agent/res/ops", {"method": "apply"}, {"yaml_content": YAML}),
    ("resource_delete", {"namespace": "ops", "resource_type": "persistentvolumeclaim", "resource_name": "data"}, "DELETE", "/api/agent/res/delete", {"namespace": "ops", "resource_type": "pvc", "resource_name": "data"}, None),
    ("statefulset_pods", {"namespace": "ops", "statefulset": "db"}, "GET", "/api/agent/statefulset/pods", {"namespace": "ops", "statefulset": "db"}, None),
    ("restart_statefulset", {"namespace": "ops", "statefulset": "db"}, "POST", "/api/agent/statefulset/restart", {}, {"namespace": "ops", "statefulset": "db"}),
    ("scale_statefulset", {"namespace": "ops", "statefulset": "db", "replicas": 0}, "POST", "/api/agent/statefulset/scale", {}, {"namespace": "ops", "statefulset": "db", "replicas": 0}),
    ("daemonset_pods", {"namespace": "ops", "daemonset": "monit"}, "GET", "/api/agent/daemonset/pods", {"namespace": "ops", "daemonset": "monit"}, None),
    ("restart_daemonset", {"namespace": "ops", "daemonset": "monit"}, "POST", "/api/agent/daemonset/restart", {}, {"namespace": "ops", "daemonset": "monit"}),
    ("image_tags", {"namespace": "ops", "deployment": "app"}, "POST", "/api/image/tags", {}, {"namespace": "ops", "deployment": "app", "k8s": ENV}),
    ("node_resource_rank", {"type": "peak_mem", "namespace": "ops", "deployment": "app"}, "GET", "/api/prom_node_rank", {"type": "peak_mem", "namespace": "ops", "deployment": "app"}, None),
    ("cci_schedule_profile", {"namespace": "ops", "deployment": "app"}, "GET", "/api/cci/schedule-profile", {"namespace": "ops", "deployment": "app"}, None),
    ("load_balance_analyze", {}, "POST", "/api/load-balance/analyze", {}, {}),
    ("load_balance_execute", {"migrations": [{"namespace": "ops", "pod_name": "app-1", "deployment": "app", "source_node": "n1", "target_node": "n2", "cpu_used": 0.3, "env": "other", "path": "/admin"}]}, "POST", "/api/load-balance/execute", {}, {"migrations": [{"namespace": "ops", "pod_name": "app-1", "deployment": "app", "source_node": "n1", "target_node": "n2", "cpu_used": 0.3}]}),
    ("load_balance_isolated", {"exclude_namespaces": ["kube-system", "ops"]}, "GET", "/api/load-balance/isolated", {"exclude_namespaces": "kube-system,ops"}, None),
    ("load_balance_cleanup", {"pods": [{"namespace": "ops", "name": "app-old", "env": "other"}]}, "POST", "/api/load-balance/cleanup", {}, {"pods": [{"namespace": "ops", "name": "app-old"}]}),
    ("load_balance_node_cpu", {}, "GET", "/api/load-balance/nodes-cpu", {}, None),
    ("load_balance_check_pods", {"pods": [{"namespace": "ops", "pod_name": "app-new", "env": "other"}]}, "POST", "/api/load-balance/check-pods", {}, {"pods": [{"namespace": "ops", "pod_name": "app-new"}]}),
    ("stored_namespaces", {}, "GET", "/api/db/res/namespaces", {}, None),
    ("stored_deployments", {"namespace": "ops"}, "GET", "/api/db/res/deployments", {"namespace": "ops"}, None),
    ("resource_max_day", {}, "GET", "/api/db/res/max_day", {}, None),
    ("resource_config_update", {"namespace": "ops", "deployment": "app", "limit_mem_mb": 512, "limit_cpu_m": 500, "pod_count_manual": 2, "jvm_xms_bytes": 0, "jvm_xmx_bytes": None}, "POST", "/api/db/res/edit", {}, {"env": ENV, "namespace": "ops", "deployment": "app", "limit_mem_mb": 512, "limit_cpu_m": 500, "pod_count_manual": 2, "jvm_xms_bytes": 0, "jvm_xmx_bytes": None}),
    ("resource_pod_count", {"namespace": "ops", "deployment": "app", "pod_count_manual": 0}, "POST", "/api/db/res/pod_count", {}, {"env": ENV, "namespace": "ops", "deployment_name": "app", "pod_count_manual": 0}),
    ("alert_total", {"startTime": "2026-10-01T00:00:00+08:00"}, "POST", "/api/db/alert/total", {}, {"startTime": "2026-10-01T00:00:00+08:00", "env": ENV}),
    ("alert_detail", {"namespace": "ops", "alertName": ["PodCrash"], "status": ["firing"], "page": 2, "pageSize": 30}, "POST", "/api/db/alert/detail", {}, {"namespace": "ops", "alertName": ["PodCrash"], "status": ["firing"], "page": 2, "pageSize": 30, "env": [ENV]}),
    ("alert_detail_total", {"namespace": "ops", "silenced": "0"}, "POST", "/api/db/alert/detail_total", {}, {"namespace": "ops", "silenced": "0", "env": [ENV]}),
    ("event_history", {"start_time": "2026-10-01", "end_time": "2026-10-05", "reporting_component": "kubelet", "reporting_instance": "n1", "limit": 50}, "POST", "/api/events/query", {}, {"start_time": "2026-10-01", "end_time": "2026-10-05", "reportingComponent": "kubelet", "reportingInstance": "n1", "limit": 50, "k8s": ENV}),
]


def context(permission="rw"):
    return RunContext({"id": "run-1", "session_id": "session-1", "scope": {"env": ENV, "namespace": "", "deployment": None, "pod": None}}, Identity("user", permission))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,args,method,path,query,body", CONTRACTS, ids=[row[0] for row in CONTRACTS])
async def test_registered_business_operations_use_existing_http_contract(operation, args, method, path, query, body):
    """Exercise Gateway dispatch, not just the mapping helper's implementation."""
    args = {**copy.deepcopy(args), "url": "http://arbitrary", "path": "/api/load-balance/config", "api_key": "must-not-forward"}
    seen = []

    def receive(request):
        seen.append(request)
        assert request.method == method
        assert str(request.url).split("?")[0] == "http://master" + path
        assert dict(request.url.params) == {"env": ENV, **query}
        assert (json.loads(request.content) if request.content else None) == body
        assert request.headers["X-User-Name"] == "user"
        return httpx.Response(200, json={"success": True, "data": [["ops"]], "meta": [{"name": "namespace"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    gateway = Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "internal-token", http=client)
    try:
        assert CATALOG[operation][:2] == (method, path)
        assert await gateway.choose_source(context(), operation, "auto") == "kubedoor"
        result = await gateway.execute_action(context(), {"source": "kubedoor", "operation": operation, "arguments": args})
        assert result["success"] is True
        assert result["source"] == "kubedoor"
        assert result["data"] == [{"namespace": "ops"}]
        assert len(seen) == 1
    finally:
        await gateway.close()


@pytest.mark.parametrize("operation,args,method,path,query,body", CONTRACTS, ids=[row[0] for row in CONTRACTS])
def test_other_cluster_identifiers_are_rejected(operation, args, method, path, query, body):
    for field in ("env", "k8s"):
        with pytest.raises(AIError) as denied:
            build_request(operation, {**args, field: "other-cluster"}, ENV)
        assert denied.value.code == "scope_violation"
    if operation in ("alert_detail", "alert_detail_total"):
        with pytest.raises(AIError):
            build_request(operation, {**args, "env": [ENV, "other-cluster"]}, ENV)


MUTATIONS = {
    "delete_pods", "cordon_nodes", "uncordon_nodes", "resource_apply", "resource_delete",
    "restart_statefulset", "scale_statefulset", "restart_daemonset", "load_balance_analyze",
    "load_balance_execute", "load_balance_cleanup",
    "resource_config_update", "resource_pod_count",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(MUTATIONS))
async def test_business_mutations_freeze_and_require_write_permission(operation):
    args = copy.deepcopy(next(row[1] for row in CONTRACTS if row[0] == operation))
    gateway = Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "internal")
    gateway.execute_action = AsyncMock()
    try:
        with pytest.raises(AIError) as denied:
            await gateway.invoke(context("read"), operation, args, call_id="denied", mcp=True)
        assert denied.value.status == 403
        assert gateway.store.actions == {}
        with pytest.raises(PendingAction) as pending:
            await gateway.invoke(context(), operation, args, call_id="approved-later", mcp=True)
        frozen = gateway.store.actions[pending.value.action["action_id"]]
        expected = copy.deepcopy(args)
        args.clear()
        assert frozen["arguments"] == expected
        assert frozen["state"] == "pending"
        assert frozen["read_only"] is False
        gateway.execute_action.assert_not_awaited()
    finally:
        await gateway.close()


INVALID = [
    ("pod_list", {"namespaces": "ops"}),
    ("pod_list", {"namespaces": ["ops,apps"]}),
    ("node_list", {"simple": "true"}),
    ("cordon_nodes", {"node_names": []}),
    ("cordon_nodes", {"node_names": [""]}),
    ("delete_pods", {"pods": [{"namespace": "ops", "pod": ""}]}),
    ("delete_pods", {"pods": []}),
    ("resource_content", {"namespace": "ops", "resource_type": "virtualservice", "resource_name": "app"}),
    ("resource_delete", {"namespace": "ops", "resource_type": "hpa", "resource_name": "app"}),
    ("resource_apply", {"yaml_content": YAML, "method": "delete"}),
    ("resource_apply", {"yaml_content": "kind: [CronJob]\napiVersion: batch/v1"}),
    ("resource_apply", {"yaml_content": "[broken"}),
    ("resource_apply", {"yaml_content": "# no documents"}),
    ("resource_apply", {"yaml_content": "apiVersion: networking.istio.io/v1\nkind: VirtualService\nmetadata: {name: app}"}),
    ("resource_apply", {"yaml_content": "apiVersion: batch/v1\nkind: CronJob\nmetadata: {}"}),
    ("scale_statefulset", {"namespace": "ops", "statefulset": "db", "replicas": -1}),
    ("scale_statefulset", {"namespace": "ops", "statefulset": "db", "replicas": True}),
    ("scale_statefulset", {"namespace": "ops", "statefulset": "db", "replicas": 1.2}),
    ("node_resource_rank", {"type": "disk"}),
    ("load_balance_execute", {"migrations": []}),
    ("load_balance_execute", {"migrations": [{"namespace": "ops", "pod_name": "app", "deployment": "app", "target_node": "n2", "cpu_used": -1}]}),
    ("load_balance_execute", {"migrations": [{"namespace": "ops", "pod_name": "app", "deployment": "app", "target_node": "n2", "cpu_used": float("nan")}]}),
    ("load_balance_cleanup", {"pods": [{"namespace": "ops", "pod_name": "wrong-key"}]}),
    ("alert_detail", {"pageSize": 501}),
    ("alert_detail", {"page": 0}),
    ("alert_detail", {"status": "firing"}),
    ("alert_detail", {"status": ["invalid"]}),
    ("resource_pod_count", {"namespace": "ops", "deployment": "app", "pod_count_manual": -1}),
    ("resource_config_update", {"namespace": "ops", "deployment": "app", "limit_mem_mb": 512, "limit_cpu_m": 500, "pod_count_manual": 2, "jvm_xms_bytes": 1024, "jvm_xmx_bytes": 512}),
    ("resource_config_update", {"namespace": "ops", "deployment": "app", "limit_mem_mb": 512, "limit_cpu_m": 500, "pod_count_manual": 2, "jvm_xms_bytes": 9007199254740992}),
    ("alert_total", {"startTime": "yesterday"}),
    ("event_history", {"start_time": "2026-10-05", "end_time": "2026-10-01"}),
    ("event_history", {"start_time": "20261001", "end_time": "2026-10-05"}),
    ("event_history", {"start_time": "2026-10-01", "end_time": "2026-10-05", "limit": 0}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,args", INVALID)
async def test_invalid_calls_fail_before_approval_and_http(operation, args):
    gateway = Gateway(FakeStore(), CredentialCipher("ab" * 32), "http://master", "internal")
    gateway.execute_action = AsyncMock()
    try:
        with pytest.raises(AIError):
            await gateway.invoke(context(), operation, args, call_id="bad-call", mcp=True)
        assert gateway.store.actions == {}
        gateway.execute_action.assert_not_awaited()
    finally:
        await gateway.close()


def test_catalogue_has_exact_permission_classes_and_excludes_global_routes():
    assert set(EXTRA_CATALOG) == set(EXTRA_SCHEMAS)
    assert {name for name, spec in EXTRA_CATALOG.items() if not spec[3]} == MUTATIONS
    assert not {spec[1] for spec in EXTRA_CATALOG.values()} & {
        "/api/load-balance/config", "/api/load-balance/status", "/api/load-balance/plan",
        "/api/db/alert/update_operate", "/api/istio/import", "/api/istio/template",
    }


def test_request_defaults_and_source_arguments_are_not_mutated():
    args = {"namespace": "ops", "namespaces": [], "path": "http://arbitrary"}
    assert build_request("pod_list", args, ENV) == ({"env": ENV}, None)
    assert args["namespaces"] == []
    assert build_request("pod_list", {"namespace": "ops"}, ENV) == ({"env": ENV, "namespaces": "ops"}, None)
    assert build_request("node_resource_rank", {}, ENV)[0]["type"] == "cpu"
    assert build_request("resource_apply", {"yaml_content": YAML}, ENV)[0]["method"] == "apply"
    assert build_request("resource_content", {"namespace": "ops", "resource_type": "pvc", "resource_name": "data"}, ENV)[0]["resource_type"] == "persistentvolumeclaim"
    assert build_request("alert_detail", {}, ENV)[1] == {"env": [ENV], "page": 1, "pageSize": 20}


def agent_module(filename):
    # Import the actual existing handlers with mocked Kubernetes clients. These
    # dependencies are agent dependencies, so optional in the AI-only image.
    pytest.importorskip("kubernetes_asyncio")
    pytest.importorskip("aiohttp")
    path = Path(__file__).resolve().parents[2] / "kubedoor-agent" / filename
    spec = importlib.util.spec_from_file_location("contract_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,handler,desired", [("cordon_nodes", "cordon_nodes", True), ("uncordon_nodes", "uncordon_nodes", False)])
async def test_node_mapping_is_accepted_by_actual_agent_handler(operation, handler, desired, monkeypatch):
    # Cordon does not use the disk/metrics parsers imported by this module.
    monkeypatch.setitem(sys.modules, "utils", SimpleNamespace(**{name: Mock() for name in ("parse_cpu", "parse_memory", "parse_storage_to_gib", "bytes_to_gib", "parse_pods")}))
    module = agent_module("res_manager/node_manager.py")
    query, body = build_request(operation, {"node_names": ["n1"]}, ENV)
    node = SimpleNamespace(spec=SimpleNamespace(unschedulable=not desired))
    core = SimpleNamespace(read_node=AsyncMock(return_value=node), patch_node=AsyncMock())
    response = await getattr(module, handler)(core, SimpleNamespace(query=query, json=AsyncMock(return_value=body)))
    assert response.status == 200 and json.loads(response.body)["success"]
    assert node.spec.unschedulable is desired
    core.patch_node.assert_awaited_once_with(name="n1", body=node)


@pytest.mark.asyncio
async def test_zero_statefulset_replicas_maps_to_actual_agent_scale_handler():
    module = agent_module("res_manager/stateful_daemon_manager.py")
    query, body = build_request("scale_statefulset", {"namespace": "ops", "statefulset": "db", "replicas": 0}, ENV)
    workload = SimpleNamespace(spec=SimpleNamespace(replicas=3))
    apps = SimpleNamespace(read_namespaced_stateful_set=AsyncMock(return_value=workload), patch_namespaced_stateful_set_scale=AsyncMock())
    response = await module.scale_statefulset(SimpleNamespace(query=query, json=AsyncMock(return_value=body)), apps)
    assert response.status == 200 and json.loads(response.body)["success"]
    assert workload.spec.replicas == 0
    apps.patch_namespaced_stateful_set_scale.assert_awaited_once_with("db", "ops", workload)


@pytest.mark.asyncio
async def test_cronjob_apply_maps_to_actual_resource_handler(monkeypatch):
    module = agent_module("func_manager/k8s_resource_handler.py")
    manager = SimpleNamespace(execute_operation=AsyncMock(return_value=[{"status": "success", "kind": "CronJob", "name": "backup"}]))
    monkeypatch.setattr(module, "K8sResourceManager", Mock(return_value=manager))
    monkeypatch.setattr(module.client, "ApiClient", Mock())
    query, body = build_request("resource_apply", {"yaml_content": YAML}, ENV)
    response = await module.handle_k8s_operation(SimpleNamespace(query=query, json=AsyncMock(return_value=body)))
    assert response.status == 200
    assert json.loads(response.body)["success"]
    manager.execute_operation.assert_awaited_once_with("apply", YAML)


def master_db_module(monkeypatch):
    pytest.importorskip("aiohttp")
    db = ModuleType("db")
    db.pg_fetch = AsyncMock(return_value=[])
    db.pg_fetchval = AsyncMock(return_value=0)
    db.pg_execute = AsyncMock()
    monkeypatch.setitem(sys.modules, "db", db)
    utils = ModuleType("utils")
    utils.invalidate_admis_cache = Mock()
    monkeypatch.setitem(sys.modules, "utils", utils)
    path = Path(__file__).resolve().parents[2] / "kubedoor-master/func_manager/db_api.py"
    spec = importlib.util.spec_from_file_location("catalogue_master_db_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, db


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,handler", [("alert_total", "alert_total"), ("alert_detail", "alert_detail"), ("alert_detail_total", "alert_detail_total")])
async def test_alert_mappings_keep_database_query_on_one_cluster(operation, handler, monkeypatch):
    module, db = master_db_module(monkeypatch)
    query, body = build_request(operation, {}, ENV)
    response = await getattr(module, handler)(SimpleNamespace(query=query, json=AsyncMock(return_value=body)))
    assert response.status == 200
    sql, *parameters = db.pg_fetch.await_args.args
    assert parameters[0] == ENV
    assert "env = $1" in sql if operation == "alert_total" else "env IN ($1)" in sql


@pytest.mark.asyncio
async def test_resource_database_update_explicitly_clears_jvm_and_keeps_cluster_filter(monkeypatch):
    module, db = master_db_module(monkeypatch)
    query, body = build_request("resource_config_update", {"namespace": "ops", "deployment": "app", "limit_mem_mb": 512, "limit_cpu_m": 500, "pod_count_manual": 2, "jvm_xmx_bytes": None}, ENV)
    response = await module.res_edit(SimpleNamespace(query=query, json=AsyncMock(return_value=body)))
    assert response.status == 200
    sql, *parameters = db.pg_execute.await_args.args
    assert parameters == [512, 500, 2, None, ENV, "ops", "app"]
    assert "jvm_xmx_bytes=$4" in sql
    assert "jvm_xms_bytes=" not in sql  # Omitted fields retain their stored value.
    assert "WHERE env=$5 AND namespace=$6 AND deployment=$7" in sql
