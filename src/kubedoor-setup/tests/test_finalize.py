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


def owned(kind, name, version="v1"):
    obj = resource(kind, name, "kubedoor", version)
    obj["metadata"].update(annotations={OWNER: ID}, resourceVersion="1")
    return obj


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
        self.rules = next(obj["rules"] for obj in render_bootstrap("--namespace", self.namespace)
                          if obj["kind"] == "Role" and obj["metadata"]["name"] == "kubedoor-setup-finalizer")
        for kind, name, version in (("Deployment", "kubedoor-setup", "apps/v1"),
                                    ("RoleBinding", "kubedoor-setup", "rbac.authorization.k8s.io/v1"),
                                    ("Secret", "kubedoor-auth-init", "v1"),
                                    ("Secret", "kubedoor-setup-auth", "v1"),
                                    ("Secret", "kubedoor-setup-config", "v1")):
            obj = owned(kind, name, version)
            if kind == "Deployment":
                obj["metadata"]["generation"] = 4
                obj["spec"] = {"replicas": 1, "template": {"spec": {"containers": [{"name": "setup", "image": "example/setup:1"}]}}}
                obj["status"] = {"observedGeneration": 4, "replicas": 1}
            elif kind == "Secret":
                obj["data"] = {"password": "aW5pdGlhbC1wYXNzd29yZA=="}
            self.objects[resource_path(obj)] = obj

    def authorize(self, verb, path):
        segments = path.split("/")
        name, plural = segments[-1], segments[-2]
        group = "" if segments[1] == "api" else segments[2]
        assert any(group in rule["apiGroups"] and plural in rule["resources"] and verb in rule["verbs"]
                   and name in rule.get("resourceNames", []) for rule in self.rules), (verb, path)
        self.calls.append((verb, path))

    async def get(self, obj):
        path = resource_path(obj)
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
        assert method == "PATCH"
        self.authorize("patch", path)
        assert apply and params == {"fieldManager": "kubedoor-setup-finalizer", "force": "true"}
        assert body["spec"] == {"replicas": 0}
        self.events.append(("scale", "kubedoor-setup"))
        obj = self.objects[path]
        obj["spec"]["replicas"] = 0
        obj["metadata"]["generation"] += 1
        return copy.deepcopy(obj)

    async def delete(self, obj):
        path = resource_path(obj)
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
    initial = k8s.objects[resource_path(owned("Secret", "kubedoor-auth-init"))]
    assert initial["data"] == {}  # Keep the referenced Secret so restarts still start.
    assert repo.state["private_key"] is None and "error" not in repo.state
    assert next(row for row in repo.state["checks"] if row["name"] == "cleanup")["status"] == "success"
    assert repo.state["completed_at"]
    previous = list(events)
    await module.finalize(repo, k8s, ID)
    assert events == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", ["service_failed", "empty_services", "missing_required_service", "skipped_required_service", "skipped_selected_optional", "missing_selection", "pending_endpoint", "pending_write", "skipped_required_write", "wrong_id", "wrong_phase"])
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
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert events == [] and k8s.calls == []


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
                                               ("RoleBinding", "kubedoor-setup", "rbac.authorization.k8s.io/v1")])
async def test_foreign_resources_are_not_removed(env, kind, name, version):
    repo, k8s, events = env
    path = resource_path(owned(kind, name, version))
    k8s.objects[path]["metadata"]["annotations"][OWNER] = "another-installation"
    with pytest.raises(SetupError):
        await module.finalize(repo, k8s, ID)
    assert path in k8s.objects
    assert repo.state["phase"] == "finalizing"
    assert ("save", "succeeded") not in events


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
@pytest.mark.parametrize('name', ['kubedoor-setup', 'kubedoor-setup-config', 'kubedoor-setup-auth'])
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
