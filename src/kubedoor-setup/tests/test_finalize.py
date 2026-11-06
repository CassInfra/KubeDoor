"""Finalization must prove ownership and cleanup before publishing success."""
import copy
from pathlib import Path

import pytest

import kubedoor_setup.finalize as module
from kubedoor_setup.errors import SetupError
from kubedoor_setup.kubernetes import OWNER, resource, resource_path
from bootstrap_shell import render_bootstrap
from kubedoor_setup.repository import new_state


ROOT = Path(__file__).resolve().parents[3]
ID = "718df732-9671-4144-8033-1c531ee41813"
RBAC = "rbac.authorization.k8s.io/v1"
INSTALLER = ("ServiceAccount", "kubedoor-agent-installer", "v1")
CLUSTER_BINDING = ("ClusterRoleBinding", "kubedoor-agent-installer-kubedoor", RBAC)
TOKEN_PATH = "/api/v1/namespaces/kubedoor/serviceaccounts/kubedoor-agent-installer/token"


def owned(kind, name, version="v1"):
    obj = resource(kind, name, "kubedoor", version)
    obj["metadata"].update(annotations={OWNER: ID}, resourceVersion="1")
    if kind == "ClusterRoleBinding":
        del obj["metadata"]["namespace"]
        obj["subjects"] = [{"kind": "ServiceAccount", "name": "kubedoor-agent-installer", "namespace": "kubedoor"}]
        obj["roleRef"] = {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"}
    return obj


def path_of(kind, name, version):
    return resource_path(owned(kind, name, version))


class MemoryRepo:
    def __init__(self, events):
        self.events = events
        self.state = new_state(ID, "kubedoor")
        self.state.update(phase="finalizing", private_key="private-old.json", error="old error")
        self.state["services"] = [{"id": name, "status": "ready"} for name in ("web", "master", "ai", "alarm", "grafana", "vmalert", "alertmanager")]
        self.state['selected_services'] = [row['id'] for row in self.state['services']]
        self.state['metrics_write_required'] = True
        self.state["checks"] = [{"name": name, "status": "success"} for name in ("postgres", "metrics", "resources", "endpoints", "write")]
        self.state["checks"].append({"name": "cleanup", "status": "running"})
        self.before_last_read = None
        self.reads = 0
        self.fail_next_save = False

    async def load(self):
        self.reads += 1
        if self.reads == 2 and self.before_last_read:
            self.before_last_read(self.state)
        return copy.deepcopy(self.state)

    async def save(self, state):
        if self.fail_next_save:
            self.fail_next_save = False
            raise SetupError("Concurrent status update", 409)
        self.events.append(("save", state["phase"]))
        self.state = copy.deepcopy(state)
        return copy.deepcopy(self.state)


class PolicyKubernetes:
    namespace = "kubedoor"

    def __init__(self, events):
        self.events = events
        self.calls = []
        self.fail_delete = None
        self.defer_delete = None
        self.stuck = False
        self.pending_reads = 0
        self.objects = {}
        self.tokens = set()
        self.rules = next(obj["rules"] for obj in render_bootstrap("--namespace", self.namespace)
                          if obj["kind"] == "Role" and obj["metadata"]["name"] == "kubedoor-setup-finalizer")
        for kind, name, version in (("Deployment", "kubedoor-setup", "apps/v1"),
                                    ("RoleBinding", "kubedoor-setup", "rbac.authorization.k8s.io/v1"),
                                    ("Secret", "kubedoor-auth-init", "v1"),
                                    ("Secret", "kubedoor-setup-auth", "v1"),
                                    ("Secret", "kubedoor-setup-config", "v1"),
                                    INSTALLER, CLUSTER_BINDING):
            obj = owned(kind, name, version)
            if kind == "Deployment":
                obj["metadata"]["generation"] = 4
                obj["spec"] = {"replicas": 1, "template": {"spec": {"containers": [{"name": "setup", "image": "example/setup:1"}]}}}
                obj["status"] = {"observedGeneration": 4, "replicas": 1}
            elif kind == "Secret":
                obj["data"] = {"password": "aW5pdGlhbC1wYXNzd29yZA=="}
            self.objects[resource_path(obj)] = obj
        # 引导脚本记在状态 ConfigMap 里的标记：本次引导授予了 Agent 安装账号
        marker = owned("ConfigMap", "kubedoor-setup-state")
        marker["data"] = {"agent-installer": "kubedoor-agent-installer-kubedoor"}
        self.objects[resource_path(marker)] = marker

    def authorize(self, verb, path):
        """按引导脚本里收尾账号的 Role 判断（含 serviceaccounts/token 子资源）。"""
        segments = path.strip("/").split("/")
        group, rest = ("", segments[2:]) if segments[0] == "api" else (segments[1], segments[3:])
        if rest[0] == "namespaces":
            rest = rest[2:]
        plural, name = rest[0], rest[1]
        if len(rest) > 2:
            plural += "/" + rest[2]
        assert any(group in rule["apiGroups"] and plural in rule["resources"] and verb in rule["verbs"]
                   and name in rule.get("resourceNames", []) for rule in self.rules), (verb, path)
        self.calls.append((verb, path))

    def installer(self, token, path):
        """安装账号令牌的权限只来自它的 cluster-admin 绑定：绑定删掉后立即 403。"""
        assert token in self.tokens
        binding = self.objects.get(path_of(*CLUSTER_BINDING))
        if not binding or path_of(*INSTALLER) not in self.objects:
            raise SetupError("安装权限不足或准入策略拒绝（HTTP 403）", 403)
        self.calls.append(("installer", path))

    async def get(self, obj, token=None):
        path = resource_path(obj)
        if token is not None:
            self.installer(token, path)
        else:
            self.authorize("get", path)
        found = self.objects.get(path)
        if found and found["kind"] == "Deployment" and found["spec"]["replicas"] == 0 and not self.stuck:
            if self.pending_reads:
                self.pending_reads -= 1
            else:
                found["status"] = {"observedGeneration": found["metadata"]["generation"], "replicas": 0}
        return copy.deepcopy(found)

    async def replace(self, obj):
        path = resource_path(obj)
        self.authorize("update", path)
        self.events.append(("update", obj["metadata"]["name"]))
        self.objects[path] = copy.deepcopy(obj)

    async def request(self, method, path, *, body, apply=False, params=None):
        if method == "POST":
            assert path == TOKEN_PATH and body["kind"] == "TokenRequest"
            self.authorize("create", path)
            assert path_of(*INSTALLER) in self.objects
            token = "installer-token-%d" % len(self.tokens)
            self.tokens.add(token)
            self.events.append(("token", "kubedoor-agent-installer"))
            return {"status": {"token": token}}
        assert method == "PATCH"
        self.authorize("patch", path)
        assert apply and params == {"fieldManager": "kubedoor-setup-finalizer", "force": "true"}
        assert body["spec"] == {"replicas": 0}
        self.events.append(("scale", "kubedoor-setup"))
        obj = self.objects[path]
        obj["spec"]["replicas"] = 0
        obj["metadata"]["generation"] += 1
        return copy.deepcopy(obj)

    async def delete(self, obj, token=None):
        path = resource_path(obj)
        if token is not None:
            self.installer(token, path)
        else:
            self.authorize("delete", path)
        if self.fail_delete == obj["metadata"]["name"]:
            raise SetupError("Deletion denied", 403)
        self.events.append(("delete", obj["metadata"]["name"]))
        if self.defer_delete == obj['metadata']['name']:
            self.objects[path]['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
            return
        self.objects.pop(path, None)


@pytest.fixture
def env(monkeypatch):
    async def no_sleep(seconds):
        return None
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    events = []
    return MemoryRepo(events), PolicyKubernetes(events), events


@pytest.mark.asyncio
async def test_success_requires_all_cleanup_and_bootstrap_rbac_allows_operations(env):
    repo, k8s, events = env
    k8s.pending_reads = 2
    await module.finalize(repo, k8s, ID)
    assert events[-1] == ("save", "succeeded")
    assert events.index(("update", "kubedoor-auth-init")) < events.index(("scale", "kubedoor-setup"))
    assert events.index(("scale", "kubedoor-setup")) < events.index(("delete", "kubedoor-setup"))
    assert ("delete", "kubedoor-setup-config") in events and ("delete", "kubedoor-setup-auth") in events
    # 安装服务停止后才撤销集群权限：先用安装账号令牌删绑定，再删账号，最后删安装服务的 RoleBinding
    order = [events.index(item) for item in (("scale", "kubedoor-setup"), ("token", "kubedoor-agent-installer"),
                                              ("delete", "kubedoor-agent-installer-kubedoor"),
                                              ("delete", "kubedoor-agent-installer"), ("delete", "kubedoor-setup"))]
    assert order == sorted(order)
    assert path_of(*CLUSTER_BINDING) not in k8s.objects and path_of(*INSTALLER) not in k8s.objects
    # 集群级绑定只用安装账号自己的令牌访问，收尾账号没有任何集群级权限
    assert ("installer", path_of(*CLUSTER_BINDING)) in k8s.calls
    assert not any(verb != "installer" and "clusterrolebindings" in path for verb, path in k8s.calls)
    initial = k8s.objects[resource_path(owned("Secret", "kubedoor-auth-init"))]
    assert initial["data"] == {}  # Keep the referenced Secret so restarts still start.
    assert repo.state["private_key"] is None and "error" not in repo.state
    assert next(row for row in repo.state["checks"] if row["name"] == "cleanup")["status"] == "success"
    assert repo.state["completed_at"]
    previous = list(events)
    await module.finalize(repo, k8s, ID)
    assert events == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", ["service_failed", "empty_services", "missing_required_service", "skipped_required_service", "skipped_selected_optional", "missing_selection", "pending_endpoint", "pending_write", "skipped_required_write", "wrong_id", "wrong_phase", "agent_running", "agent_choice"])
async def test_incomplete_installation_never_runs_cleanup(env, incomplete):
    repo, k8s, events = env
    if incomplete == "service_failed": repo.state["services"][0]["status"] = "failed"
    if incomplete == "empty_services": repo.state["services"] = []
    if incomplete == 'missing_required_service': repo.state['services'] = repo.state['services'][1:]
    if incomplete == 'skipped_required_service': repo.state['services'][0]['status'] = 'skipped'
    if incomplete == 'skipped_selected_optional': repo.state['services'][-1]['status'] = 'skipped'
    if incomplete == 'missing_selection': repo.state.pop('selected_services')
    if incomplete == "pending_endpoint": repo.state["checks"][3]["status"] = "running"
    if incomplete == "pending_write": repo.state["checks"][4]["status"] = "running"
    if incomplete == 'skipped_required_write': repo.state['checks'][4]['status'] = 'skipped'
    if incomplete == "wrong_id": repo.state["id"] = "different"
    if incomplete == "wrong_phase": repo.state["phase"] = "failed"
    if incomplete == "agent_running": repo.state["agent"] = {"status": "running"}
    if incomplete == "agent_choice": repo.state["agent"] = {"status": "choice"}
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert events == [] and k8s.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["succeeded", "partial", "skipped", "unavailable"])
async def test_concluded_agent_step_can_finish(env, agent):
    repo, k8s, _ = env
    repo.state["agent"] = {"status": agent, "options": {"env": "prod"}}
    await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "succeeded"


@pytest.mark.asyncio
async def test_optional_unselected_services_and_query_only_can_finish(env):
    repo, k8s, _ = env
    repo.state["services"][-1]["status"] = "skipped"
    repo.state['selected_services'].remove(repo.state['services'][-1]['id'])
    repo.state['metrics_write_required'] = False
    repo.state["checks"][4]["status"] = "skipped"
    await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,name,version", [("Deployment", "kubedoor-setup", "apps/v1"),
                                               ("Secret", "kubedoor-auth-init", "v1"),
                                               ("Secret", "kubedoor-setup-auth", "v1"),
                                               ("Secret", "kubedoor-setup-config", "v1"),
                                               ("RoleBinding", "kubedoor-setup", "rbac.authorization.k8s.io/v1"),
                                               INSTALLER, CLUSTER_BINDING])
async def test_foreign_resources_are_not_removed(env, kind, name, version):
    repo, k8s, events = env
    path = resource_path(owned(kind, name, version))
    k8s.objects[path]["metadata"]["annotations"][OWNER] = "another-installation"
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert path in k8s.objects
    assert repo.state["phase"] == "finalizing"
    assert ("save", "succeeded") not in events
    if kind in ("ServiceAccount", "ClusterRoleBinding"):
        # 归属不符时安装账号和安装服务的 RoleBinding 都原样保留
        assert path_of(*INSTALLER) in k8s.objects
        assert ("delete", "kubedoor-setup") not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [(), (INSTALLER,)])
async def test_revoke_skips_missing_grant_and_removes_unbound_account(env, present):
    # 从未授予（旧版本引导），或授予失败 / 上次收尾已删掉绑定：都不阻断收尾
    repo, k8s, events = env
    for item in (INSTALLER, CLUSTER_BINDING):
        if item not in present:
            k8s.objects.pop(path_of(*item))
    await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "succeeded"
    assert path_of(*INSTALLER) not in k8s.objects
    assert (("token", "kubedoor-agent-installer") in events) is bool(present)
    assert ("delete", "kubedoor-agent-installer-kubedoor") not in events


@pytest.mark.asyncio
async def test_bootstrap_without_agent_installer_marker_never_touches_accounts(env):
    # 旧版本脚本引导、新版本安装服务：没有账号也没有对应权限，收尾照常完成
    repo, k8s, events = env
    k8s.rules = [rule for rule in k8s.rules if not any(name.startswith("serviceaccounts") for name in rule["resources"])]
    for item in (INSTALLER, CLUSTER_BINDING):
        k8s.objects.pop(path_of(*item))
    k8s.objects[path_of("ConfigMap", "kubedoor-setup-state", "v1")]["data"] = {}
    await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "succeeded"
    assert not any("serviceaccounts" in path for _, path in k8s.calls)


@pytest.mark.asyncio
async def test_revoke_failure_keeps_account_and_installer_access_for_retry(env):
    repo, k8s, events = env
    k8s.fail_delete = "kubedoor-agent-installer-kubedoor"
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    # 绑定删不掉时不删账号（否则以后同名账号重建会继承绑定），也不撤销安装服务的权限
    assert path_of(*INSTALLER) in k8s.objects and path_of(*CLUSTER_BINDING) in k8s.objects
    assert ("delete", "kubedoor-setup") not in events
    k8s.fail_delete = None
    await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "succeeded"
    assert path_of(*INSTALLER) not in k8s.objects and path_of(*CLUSTER_BINDING) not in k8s.objects


@pytest.mark.asyncio
async def test_waits_for_old_installer_pods_before_revoking_access(env):
    repo, k8s, events = env
    k8s.stuck = True
    with pytest.raises(SetupError) as error:
        await module.finalize(repo, k8s, ID)
    assert error.value.status == 503
    assert not any(verb == "delete" for verb, _ in events)
    assert repo.state["phase"] == "finalizing"


@pytest.mark.asyncio
async def test_cleanup_failure_and_retry_are_idempotent(env):
    repo, k8s, events = env
    k8s.fail_delete = "kubedoor-setup-auth"
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "finalizing"
    assert ("save", "succeeded") not in events
    k8s.fail_delete = None
    await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "succeeded"
    assert events.count(("scale", "kubedoor-setup")) == 1


@pytest.mark.asyncio
async def test_final_status_write_conflict_can_retry_after_credentials_removed(env):
    repo, k8s, events = env
    repo.fail_next_save = True
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "finalizing"
    await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "succeeded"
    assert events.count(("scale", "kubedoor-setup")) == 1


@pytest.mark.asyncio
async def test_concurrent_installation_replacement_is_not_marked_success(env):
    repo, k8s, events = env
    repo.before_last_read = lambda state: state.update(id="another-installation", phase="draft")
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert repo.state["phase"] == "draft"
    assert ("save", "succeeded") not in events


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['kubedoor-setup', 'kubedoor-setup-config', 'kubedoor-setup-auth',
                                  'kubedoor-agent-installer-kubedoor', 'kubedoor-agent-installer'])
async def test_accepted_delete_is_not_completed_cleanup(env, name):
    repo, k8s, events = env
    k8s.defer_delete = name
    with pytest.raises(SetupError) as error:
        await module.finalize(repo, k8s, ID)
    assert error.value.status == 503
    assert repo.state['phase'] == 'finalizing'
    assert ('save', 'succeeded') not in events
    k8s.defer_delete = None
    await module.finalize(repo, k8s, ID)
    assert repo.state['phase'] == 'succeeded'


@pytest.mark.asyncio
async def test_dependency_failure_during_cleanup_is_not_marked_success(env):
    repo, k8s, events = env
    def fail_dependency(state):
        next(row for row in state['checks'] if row['name'] == 'endpoints')['status'] = 'error'
    repo.before_last_read = fail_dependency
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert repo.state['phase'] == 'finalizing'
    assert ('save', 'succeeded') not in events
