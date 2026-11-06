"""The web entry installs without a user configuration; render never executes input."""
import copy
import json
import re
import sys
from urllib.parse import quote

import pytest
import yaml

from bootstrap_shell import (ROOT, OWNER, INSTALL_ID, ShellCluster, documents, render_bootstrap, recovery_objects, state_object)

sys.path.insert(0, str(ROOT / "src/kubedoor-setup"))
from kubedoor_setup.render import load_defaults, render_master

VM_PASSWORD = "vm-secret-'$&value"
VM_AUTH = "monit:" + quote(VM_PASSWORD, safe="")


@pytest.fixture
def config():
    return {
        "postgres": {"host": "db.example.invalid", "port": 5432, "database": "kubedoor", "user": "kubedoor", "password": "pg-secret-'$&\\\nvalue", "sslmode": "require"},
        "metrics": {"type": "Victoria-Metrics-Single", "scheme": "http", "host": "metrics.example.invalid", "tenant": "0",
                    "user": "monit", "password": VM_PASSWORD,
                    "read_url": f"http://{VM_AUTH}@metrics.example.invalid:8428",
                    "write_url": f"http://{VM_AUTH}@metrics.example.invalid:8428/api/v1/write", "tag_key": "cluster"},
        "admin": {"username": "kubedoor", "password": "admin-secret-'$value"},
        "public_url": "https://kubedoor.example.invalid",
        "components": {"grafana": True, "vmalert": True, "alertmanager": True},
    }


SECRETS = {"internal_token": "a" * 64, "encryption_key": "b" * 64, "agent_password": "agent-secret", "grafana_password": "grafana-secret"}


def rendered(config):
    return {(obj["kind"], obj["metadata"]["name"]): obj for obj in render_master(config, SECRETS, "example")}


def prometheus_source(resources):
    grafana = resources["Secret", "kubedoor-grafana-datasources"]["stringData"]
    sources = yaml.safe_load(grafana["datasources.yaml"])["datasources"]
    return grafana, next(source for source in sources if source["type"] == "prometheus")


def test_metrics_endpoints_render_verbatim_and_grafana_gets_split_credentials(config):
    resources = rendered(config)
    connection = resources["Secret", "kubedoor-connection"]["stringData"]
    assert connection["PROM_URL"] == f"http://{VM_AUTH}@metrics.example.invalid:8428"
    assert connection["METRICS_WRITE_URL"] == f"http://{VM_AUTH}@metrics.example.invalid:8428/api/v1/write"
    assert resources["ConfigMap", "kubedoor-config"]["data"]["PROM_TYPE"] == "Victoria-Metrics-Single"
    grafana, source = prometheus_source(resources)
    assert (grafana["metrics-url"], grafana["metrics-user"], grafana["metrics-password"]) == ("http://metrics.example.invalid:8428", "monit", VM_PASSWORD)
    assert source["basicAuth"] is True
    args = resources["Deployment", "vmalert"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert {"-datasource.url=$(PROM_URL)", "-remoteRead.url=$(PROM_URL)", "-remoteWrite.url=$(METRICS_WRITE_URL)"} <= set(args)


def test_cluster_endpoints_without_account_render_without_basic_auth(config):
    config["metrics"].update(type="Victoria-Metrics-Cluster", tenant="3", user="", password="",
                             read_url="https://vm.example.invalid:8481/select/3/prometheus",
                             write_url="https://vm.example.invalid:8480/insert/3/prometheus")
    resources = rendered(config)
    connection = resources["Secret", "kubedoor-connection"]["stringData"]
    assert connection["PROM_URL"] == "https://vm.example.invalid:8481/select/3/prometheus"
    assert connection["METRICS_WRITE_URL"] == "https://vm.example.invalid:8480/insert/3/prometheus"
    assert resources["ConfigMap", "kubedoor-config"]["data"]["PROM_TYPE"] == "Victoria-Metrics-Cluster"
    grafana, source = prometheus_source(resources)
    assert (grafana["metrics-url"], grafana["metrics-user"], grafana["metrics-password"]) == (connection["PROM_URL"], "", "")
    assert source["basicAuth"] is False


def test_metrics_config_saved_by_previous_wizard_still_renders(config):
    config["metrics"] = {"type": "Victoria-Metrics-Single", "url": "http://metrics.example.invalid:8428", "user": "monit",
                         "password": VM_PASSWORD, "write_url": "", "tag_key": "cluster"}
    connection = rendered(config)["Secret", "kubedoor-connection"]["stringData"]
    assert connection["PROM_URL"] == f"http://{VM_AUTH}@metrics.example.invalid:8428"
    assert connection["METRICS_WRITE_URL"] == f"http://{VM_AUTH}@metrics.example.invalid:8428/api/v1/write"


def test_render_preserves_credentials_without_yaml_or_shell_injection(config):
    objects = render_master(config, SECRETS, "example", {"WEB_NODEPORT": "31234"})
    resources = {(obj["kind"], obj["metadata"]["name"]): obj for obj in objects}
    assert yaml.safe_load_all(yaml.safe_dump_all(objects))
    connection = resources["Secret", "kubedoor-connection"]["stringData"]
    assert connection["PG_PASSWORD"] == config["postgres"]["password"]
    assert connection["PG_SSLMODE"] == "require"
    assert resources["Secret", "kubedoor-auth-init"]["stringData"]["KUBEDOOR_INIT_ADMIN_PASSWORD"] == config["admin"]["password"]
    public_resources = json.dumps([obj for obj in objects if obj["kind"] != "Secret"])
    for marker in ("pg-secret", "vm-secret", "admin-secret", "agent-secret", "grafana-secret"):
        assert marker not in public_resources
    for deployment in ("kubedoor-master", "kubedoor-alarm", "kubedoor-ai"):
        container = resources["Deployment", deployment]["spec"]["template"]["spec"]["containers"][0]
        assert {"secretRef": {"name": "kubedoor-connection"}} in container["envFrom"]
    assert resources["Service", "kubedoor-web"]["spec"]["ports"][0]["nodePort"] == 31234
    assert "kubedoor-dash-k8s" in [obj["metadata"]["name"] for obj in objects]
    assert "datasources.yaml" not in resources["ConfigMap", "kubedoor-grafana-config"]["data"]
    nginx = resources["ConfigMap", "nginx-config"]["data"]["nginx.conf"]
    assert "location = /setup-status.json" in nginx
    assert "proxy_pass http://kubedoor-setup:8080;" in nginx
    assert 'Cache-Control "no-store"' in nginx


def test_render_keeps_shared_defaults_and_excludes_unselected_components(config):
    config["components"] = {"grafana": False, "vmalert": False, "alertmanager": False}
    first = render_master(config, SECRETS, "example", {"ENABLE_MCP": "false", "TAG_MASTER": "version-test"})
    second = render_master(copy.deepcopy(config), dict(SECRETS), "example", {"ENABLE_MCP": "false", "TAG_MASTER": "version-test"})
    assert first == second
    objects = {(obj["kind"], obj["metadata"]["name"]): obj for obj in first}
    assert ("Deployment", "kubedoor-dash") not in objects
    assert ("Deployment", "vmalert") not in objects
    assert ("Deployment", "alertmanager") not in objects
    nginx = objects["ConfigMap", "nginx-config"]["data"]["nginx.conf"]
    assert "proxy_pass http://kubedoor-dash" not in nginx
    assert "location ~ ^/(mcp|sse|messages)" not in nginx
    assert objects["Deployment", "kubedoor-master"]["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":version-test")
    assert "__CONFIG_HASH" not in json.dumps(first)
    assert "__PROM_K8S_TAG_KEY__" not in json.dumps(first)


def test_vmalert_requires_selected_alertmanager(config):
    config["components"]["alertmanager"] = False
    with pytest.raises(ValueError, match="Alertmanager"):
        render_master(config, SECRETS, "example")


def test_bootstrap_has_only_web_installer_and_scoped_permissions():
    resources = render_bootstrap("--namespace", "example", "--nodeport", "31234")
    workloads = [obj for obj in resources if obj["kind"] == "Deployment"]
    assert {obj["metadata"]["name"] for obj in workloads} == {"kubedoor-web", "kubedoor-setup"}
    # 唯一的集群级资源：Agent 安装账号的 cluster-admin 绑定（第 6 步签发短期令牌使用，收尾时删除）
    cluster_scoped = [obj for obj in resources if obj["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    assert len(cluster_scoped) == 1
    binding = cluster_scoped[0]
    assert binding["kind"] == "ClusterRoleBinding"
    assert binding["metadata"]["name"] == "kubedoor-agent-installer-example"
    assert "namespace" not in binding["metadata"]
    assert binding["metadata"]["annotations"] == {OWNER: "preview"}
    assert binding["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "kubedoor-setup"
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"}
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "kubedoor-agent-installer", "namespace": "example"}]
    accounts = {obj["metadata"]["name"] for obj in resources if obj["kind"] == "ServiceAccount"}
    assert accounts == {"kubedoor-setup", "kubedoor-setup-finalizer", "kubedoor-agent-installer"}
    # 两个 RoleBinding 只绑定各自同名账号，安装账号不跑任何工作负载
    for obj in resources:
        if obj["kind"] == "RoleBinding":
            assert obj["subjects"] == [{"kind": "ServiceAccount", "name": obj["metadata"]["name"], "namespace": "example"}]
    assert all(obj["spec"]["template"]["spec"].get("serviceAccountName") != "kubedoor-agent-installer" for obj in workloads)
    roles = {obj["metadata"]["name"]: obj["rules"] for obj in resources if obj["kind"] == "Role"}
    assert all("*" not in rule["verbs"] and "*" not in rule["resources"] for rules in roles.values() for rule in rules)
    assert all(rule.get("resourceNames") for rule in roles["kubedoor-setup-finalizer"])
    # 只能给安装账号签发令牌；安装服务本身不能管理账号或 RBAC
    token_rules = [rule for rules in roles.values() for rule in rules if "serviceaccounts/token" in rule["resources"]]
    assert len(token_rules) == 2
    assert all(rule["resources"] == ["serviceaccounts/token"] and rule["resourceNames"] == ["kubedoor-agent-installer"]
               and rule["verbs"] == ["create"] for rule in token_rules)
    setup_resources = {name for rule in roles["kubedoor-setup"] for name in rule["resources"]}
    assert not {"serviceaccounts", "roles", "rolebindings", "clusterroles", "clusterrolebindings"} & setup_resources
    account_rule = next(rule for rule in roles["kubedoor-setup-finalizer"] if rule["resources"] == ["serviceaccounts"])
    assert account_rule["resourceNames"] == ["kubedoor-agent-installer"] and account_rule["verbs"] == ["get", "delete"]
    web = next(obj for obj in workloads if obj["metadata"]["name"] == "kubedoor-web")
    assert web["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    nginx = next(obj for obj in resources if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "nginx-config")["data"]["nginx.conf"]
    assert "kubedoor-master" not in nginx and "kubedoor-ai" not in nginx


@pytest.mark.parametrize("shell", ["sh", "bash", "dash"])
def test_standalone_render_uses_no_other_programs_or_configuration(tmp_path, shell):
    cluster = ShellCluster(tmp_path)
    (tmp_path / "kubedoor.conf").write_text("exit 99\n", encoding="utf-8")
    result = cluster.run("web", "--render", "--namespace", "web-test", shell=shell)
    assert result.returncode == 0, result.stderr
    objects = documents(result.stdout)
    assert objects
    assert all(obj["metadata"].get("namespace", "web-test") == "web-test" for obj in objects)
    assert not any(obj["kind"] == "Secret" for obj in objects)
    assert cluster.read()["calls"] == []


@pytest.mark.parametrize("shell", ["sh", "bash", "dash"])
def test_standalone_install_and_repeat_preserve_resources_and_credentials(tmp_path, shell):
    cluster = ShellCluster(tmp_path)
    first = cluster.run("--yes", shell=shell)
    assert first.returncode == 0, first.stderr
    saved = cluster.read()
    assert "test-install-key" in first.stdout
    assert {obj["metadata"]["name"] for obj in saved["objects"].values() if obj["kind"] == "Deployment"} == {"kubedoor-web", "kubedoor-setup"}
    for obj in saved["objects"].values():
        if obj["kind"] != "Namespace":
            assert obj["metadata"]["annotations"][OWNER] == INSTALL_ID
    mutations = len(cluster.mutations())
    second = cluster.run("--yes", "--tag", "should-not-replace", shell=shell)
    assert second.returncode == 0, second.stderr
    assert len(cluster.mutations()) == mutations
    assert cluster.read()["objects"] == saved["objects"]


def test_image_overrides_are_passed_to_the_installer(tmp_path):
    cluster = ShellCluster(tmp_path)
    result = cluster.run("--render", "--tag", "release-test", "--setup-tag", "setup-test", "--web-tag", "web-test",
                         "--image-repo", "registry.example.invalid:5443/team", "--image-pull-secret", "pull-key")
    assert result.returncode == 0, result.stderr
    resources = documents(result.stdout)
    deployments = {obj["metadata"]["name"]: obj for obj in resources if obj["kind"] == "Deployment"}
    setup = deployments["kubedoor-setup"]["spec"]["template"]["spec"]
    web = deployments["kubedoor-web"]["spec"]["template"]["spec"]
    assert setup["containers"][0]["image"] == "registry.example.invalid:5443/team/kubedoor-setup:setup-test"
    assert web["containers"][0]["image"] == "registry.example.invalid:5443/team/kubedoor-web:web-test"
    assert setup["imagePullSecrets"] == web["imagePullSecrets"] == [{"name": "pull-key"}]
    env = {item["name"]: item.get("value") for item in setup["containers"][0]["env"]}
    versions = json.loads(env["SETUP_DEFAULTS"])
    assert all(versions["TAG_" + component] == "release-test" for component in ("MASTER", "AGENT", "AI", "ALARM"))
    assert versions["IMAGE_VMAGENT"] == load_defaults()["IMAGE_VMAGENT"]


def test_editing_the_single_script_changes_published_versions(tmp_path):
    cluster = ShellCluster(tmp_path)
    source = re.sub(r'^TAG_MASTER=.*$', 'TAG_MASTER="local-edition"', cluster.script.read_text(encoding="utf-8"), flags=re.M)
    cluster.script.write_text(source, encoding="utf-8", newline="\n")
    result = cluster.run("--render")
    assert result.returncode == 0, result.stderr
    setup = next(obj for obj in documents(result.stdout) if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "kubedoor-setup")
    env = {item["name"]: item.get("value") for item in setup["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert json.loads(env["SETUP_DEFAULTS"])["TAG_MASTER"] == "local-edition"


def test_partial_bootstrap_restores_original_image_snapshot(tmp_path):
    import base64
    resources = render_bootstrap()
    objects = {}
    original_web = None
    for obj in copy.deepcopy(resources):
        metadata = obj["metadata"]
        metadata.setdefault("annotations", {})[OWNER] = INSTALL_ID
        if obj["kind"] == "Deployment" and metadata["name"] == "kubedoor-web":
            original_web = obj["spec"]["template"]["spec"]["containers"][0]["image"]
            continue
        objects[obj["kind"].lower() + "/" + metadata["name"]] = obj
    objects["secret/kubedoor-setup-auth"] = {
        "kind": "Secret", "metadata": {"name": "kubedoor-setup-auth", "annotations": {OWNER: INSTALL_ID}},
        "data": {"SETUP_KEY": base64.b64encode(b"original-bootstrap-key").decode()},
    }
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("--yes", "--tag", "unrelated-new-tag")
    assert result.returncode == 0, result.stderr
    restored = cluster.read()["objects"]["deployment/kubedoor-web"]
    assert restored["spec"]["template"]["spec"]["containers"][0]["image"] == original_web
    assert len(cluster.mutations()) == 1


def test_legacy_draft_without_snapshot_does_not_recreate_missing_resources(tmp_path):
    cluster = ShellCluster(tmp_path, {"configmap/kubedoor-setup-state": state_object("draft")})
    result = cluster.run("--yes", "--tag", "unrelated-new-tag")
    assert result.returncode != 0
    assert cluster.mutations() == []


@pytest.mark.parametrize("args", [("--namespace", "invalid/name"), ("--tag", "bad'quote"), ("--nodeport", "not-a-number"), ("--tag",)])
def test_invalid_options_never_mutate_a_cluster(tmp_path, args):
    cluster = ShellCluster(tmp_path)
    result = cluster.run(*args)
    assert result.returncode != 0
    assert cluster.mutations() == []


def test_completed_bootstrap_does_not_reopen_installer(tmp_path):
    cluster = ShellCluster(tmp_path, {"configmap/kubedoor-setup-state": state_object("succeeded")})
    result = cluster.run("--yes")
    assert result.returncode == 0, result.stderr
    assert cluster.mutations() == []
    assert not any("secret" in call["command"] for call in cluster.read()["calls"])


AGENT_KEYS = ("serviceaccount/kubedoor-agent-installer", "clusterrolebinding/kubedoor-agent-installer-kubedoor")


def test_fresh_bootstrap_grants_owned_agent_installer_with_marker(tmp_path):
    cluster = ShellCluster(tmp_path)
    result = cluster.run("--yes")
    assert result.returncode == 0, result.stderr
    assert "cluster-admin" in result.stdout
    objects = cluster.read()["objects"]
    for key in AGENT_KEYS:
        assert objects[key]["metadata"]["annotations"][OWNER] == INSTALL_ID
    binding = objects[AGENT_KEYS[1]]
    assert binding["roleRef"]["name"] == "cluster-admin" and "namespace" not in binding["metadata"]
    assert objects["configmap/kubedoor-setup-state"]["data"]["agent-installer"] == "kubedoor-agent-installer-kubedoor"
    # 集群级绑定不带 -n 应用
    applied = next(call for call in cluster.mutations() if any(obj["kind"] == "ClusterRoleBinding" for obj in call.get("objects", [])))
    assert "-n" not in applied["args"]


@pytest.mark.parametrize("phase", ["ready", "running", "failed", "interrupted", "agent", "succeeded"])
def test_started_or_completed_installation_never_regrants_cluster_permission(tmp_path, phase):
    import base64
    objects = {
        "configmap/kubedoor-setup-state": state_object(phase),
        "secret/kubedoor-setup-auth": {
            "kind": "Secret", "metadata": {"name": "kubedoor-setup-auth", "annotations": {OWNER: INSTALL_ID}},
            "data": {"SETUP_KEY": base64.b64encode(b"original-key").decode()},
        },
    }
    objects["configmap/kubedoor-setup-state"]["data"]["agent-installer"] = "kubedoor-agent-installer-kubedoor"
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("--yes")
    assert result.returncode == 0, result.stderr
    assert cluster.mutations() == []
    assert not any(key in cluster.read()["objects"] for key in AGENT_KEYS)


def bootstrap_objects(*, marker=True, skip=()):
    import base64
    objects = {}
    for obj in copy.deepcopy(render_bootstrap()):
        metadata = obj["metadata"]
        metadata.setdefault("annotations", {})[OWNER] = INSTALL_ID
        key = obj["kind"].lower() + "/" + metadata["name"]
        if key not in skip:
            objects[key] = obj
    if not marker:
        objects["configmap/kubedoor-setup-state"]["data"].pop("agent-installer")
    objects["secret/kubedoor-setup-auth"] = {
        "kind": "Secret", "metadata": {"name": "kubedoor-setup-auth", "annotations": {OWNER: INSTALL_ID}},
        "data": {"SETUP_KEY": base64.b64encode(b"draft-key").decode()},
    }
    return objects


@pytest.mark.parametrize("marker", [True, False])
def test_draft_rerun_restores_agent_installer_only_for_bootstraps_that_include_it(tmp_path, marker):
    # 旧版本引导（没有标记）的安装服务不会撤销绑定，所以绝不补建
    cluster = ShellCluster(tmp_path, bootstrap_objects(marker=marker, skip=AGENT_KEYS))
    result = cluster.run("--yes", "--tag", "unrelated-new-tag")
    assert result.returncode == 0, result.stderr
    objects = cluster.read()["objects"]
    assert all((key in objects) is marker for key in AGENT_KEYS)
    assert len(cluster.mutations()) == (2 if marker else 0)


def test_draft_rerun_refuses_foreign_cluster_binding(tmp_path):
    objects = bootstrap_objects()
    objects[AGENT_KEYS[1]]["metadata"]["annotations"][OWNER] = "another-installation"
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("--yes")
    assert result.returncode != 0
    assert cluster.mutations() == []


def test_fresh_bootstrap_refuses_leftover_cluster_binding(tmp_path):
    # 状态 ConfigMap 已被删除但集群级绑定还在（例如删了命名空间重装）：不覆盖、不接管
    leftover = {"kind": "ClusterRoleBinding", "metadata": {"name": "kubedoor-agent-installer-kubedoor", "annotations": {OWNER: "old"}}}
    cluster = ShellCluster(tmp_path, {AGENT_KEYS[1]: leftover})
    result = cluster.run("--yes")
    assert result.returncode != 0
    assert "ClusterRoleBinding:kubedoor-agent-installer-kubedoor" in result.stderr
    assert cluster.mutations() == []


def test_cluster_permission_grant_failure_does_not_block_bootstrap(tmp_path):
    cluster = ShellCluster(tmp_path, apply_error=AGENT_KEYS[1])
    result = cluster.run("--yes")
    assert result.returncode == 0, result.stderr
    assert "未能授予" in result.stderr
    assert "test-install-key" in result.stdout
    objects = cluster.read()["objects"]
    assert AGENT_KEYS[1] not in objects
    assert {"deployment/kubedoor-setup", "deployment/kubedoor-web", AGENT_KEYS[0]} <= set(objects)


def test_ready_installation_keeps_existing_configuration(tmp_path):
    import base64
    objects = {
        "configmap/kubedoor-setup-state": state_object("ready"),
        "secret/kubedoor-setup-auth": {
            "kind": "Secret", "metadata": {"name": "kubedoor-setup-auth", "annotations": {OWNER: INSTALL_ID}},
            "data": {"SETUP_KEY": base64.b64encode(b"original-ready-key").decode()},
        },
    }
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("--yes", "--tag", "unrelated-new-tag")
    assert result.returncode == 0, result.stderr
    assert "original-ready-key" in result.stdout
    assert cluster.mutations() == []


@pytest.mark.parametrize("resource", ["configmap/kubedoor-setup-state", "deployment/kubedoor-master", "deployment/kubedoor-web"])
def test_failed_cluster_reads_never_look_like_missing_resources(tmp_path, resource):
    cluster = ShellCluster(tmp_path, get_error=resource)
    result = cluster.run("--yes")
    assert result.returncode != 0
    assert cluster.mutations() == []


def test_yaml_preserves_numeric_namespace_and_boolean_like_secret_name(tmp_path):
    cluster = ShellCluster(tmp_path)
    result = cluster.run("--render", "--namespace", "123", "--image-pull-secret", "true")
    assert result.returncode == 0, result.stderr
    resources = documents(result.stdout)
    for obj in resources:
        if obj["kind"] == "Namespace":
            assert obj["metadata"]["name"] == "123"
        elif obj["kind"] == "ClusterRoleBinding":
            assert obj["metadata"]["name"] == "kubedoor-agent-installer-123"
            assert obj["subjects"][0]["namespace"] == "123"
        else:
            assert obj["metadata"]["namespace"] == "123"
        if obj["kind"] == "Deployment":
            assert obj["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "true"}]


@pytest.mark.parametrize("kind,name", [("Deployment", "kubedoor-master"), ("Deployment", "kubedoor-web"), ("ConfigMap", "nginx-config")])
def test_foreign_existing_installation_is_not_overwritten(tmp_path, kind, name):
    cluster = ShellCluster(tmp_path, {kind.lower() + "/" + name: {"kind": kind, "metadata": {"name": name, "uid": "foreign-resource"}}})
    result = cluster.run("--yes")
    assert result.returncode != 0
    assert cluster.mutations() == []


@pytest.mark.parametrize("missing", [False, True])
def test_finalizer_recovery_preserves_original_image_and_credentials(tmp_path, missing):
    objects = recovery_objects()
    if missing:
        objects.pop("job/kubedoor-setup-finalize")
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("--yes", "--tag", "new-unrelated-tag")
    assert result.returncode == 0, result.stderr
    updated = cluster.read()
    job = updated["objects"]["job/kubedoor-setup-finalize"]
    assert job["metadata"]["annotations"][OWNER] == INSTALL_ID
    assert job["spec"]["activeDeadlineSeconds"] == 600
    assert "selector" not in job["spec"] and "manualSelector" not in job["spec"]
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == "private.example/setup:original"
    assert pod["imagePullSecrets"] == [{"name": "pull-key"}]
    assert pod["serviceAccountName"] == "kubedoor-setup-finalizer"
    assert not any("secret" in call["command"] for call in updated["calls"])
    changed = [call["command"][0] for call in cluster.mutations()]
    assert changed in (["create"], ["delete", "create"], ["apply"], ["delete", "apply"])


@pytest.mark.parametrize("condition", ["Complete", "Suspended"])
def test_active_or_completed_finalizer_is_not_replaced(tmp_path, condition):
    cluster = ShellCluster(tmp_path, recovery_objects(condition))
    result = cluster.run("--yes")
    assert result.returncode == 0, result.stderr
    assert cluster.mutations() == []


@pytest.mark.parametrize("target", ["job/kubedoor-setup-finalize", "deployment/kubedoor-setup", "configmap/kubedoor-setup-state"])
def test_recovery_refuses_foreign_resources(tmp_path, target):
    objects = recovery_objects()
    objects[target]["metadata"]["annotations"][OWNER] = "foreign"
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("--yes")
    assert result.returncode != 0
    assert cluster.mutations() == []


def test_recovery_waits_for_old_job_deletion_before_creation(tmp_path):
    cluster = ShellCluster(tmp_path, recovery_objects(), delete_error=True)
    result = cluster.run("--yes")
    assert result.returncode != 0
    mutations = cluster.mutations()
    assert len(mutations) == 1 and mutations[0]["command"][0] == "delete"
    assert "--cascade=foreground" in mutations[0]["args"]
    assert "--wait=true" in mutations[0]["args"]


def test_recovery_does_not_reopen_installation_completed_during_delete(tmp_path):
    cluster = ShellCluster(tmp_path, recovery_objects(), finish_during_delete=True)
    result = cluster.run("--yes")
    assert result.returncode == 0, result.stderr
    assert [call["command"][0] for call in cluster.mutations()] == ["delete"]


