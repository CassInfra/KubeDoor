"""Exercise the standalone upgrade against a legacy cluster, including Deployment changes."""
import re
import subprocess
import sys

import pytest

from bootstrap_shell import ROOT, SCRIPT, ShellCluster, documents, state_object

TARGET = re.search(r'^TAG_MASTER="([^"]+)"', SCRIPT.read_text(encoding="utf-8"), re.M).group(1)
LEGACY_NGINX = """events {}
http {
    log_format ai_safe escape=json '$uri';
    upstream kubedoor_master { server kubedoor-master.kubedoor:80; }
    server {
        listen 80;
        location ~ ^/(api|ws)(/|$) {
            proxy_pass http://kubedoor_master;
        }
        location /custom { return 200; }
    }
}
"""


def legacy_objects(optional=True):
    objects = {}
    components = ["master", "ai", "alarm", "web"] if optional else ["master", "web"]
    for component in components:
        name = "kubedoor-" + component
        objects["deployment/" + name] = {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": 2, "selector": {"matchLabels": {"app": name}},
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1}},
                     "template": {"metadata": {"annotations": {"custom": "keep-me"}}, "spec": {
                         "nodeSelector": {"custom": "yes"}, "imagePullSecrets": [{"name": "private-pull"}],
                         "containers": [{"name": name, "image": "private.example:5443/team/" + name + ":2.3.0",
                                         "env": [{"name": "CUSTOM", "value": "do-not-change"}],
                                         "envFrom": [{"secretRef": {"name": "existing-connection"}}],
                                         "resources": {"limits": {"memory": "4Gi"}},
                                         "volumeMounts": [{"name": "custom", "mountPath": "/custom"}]},
                                        {"name": "sidecar", "image": "custom-sidecar:1"}],
                         "volumes": [{"name": "custom", "emptyDir": {}}]}}}}
    objects["configmap/nginx-config"] = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "nginx-config"},
                                        "data": {"nginx.conf": LEGACY_NGINX, "other": "keep-me"}}
    objects["configmap/kubedoor-config"] = {"kind": "ConfigMap", "metadata": {"name": "kubedoor-config"},
                                           "data": {"PG_HOST": "production-db", "UPDATE_IMAGE": "legacy-rules"}}
    objects["secret/kubedoor-ai-security"] = {"kind": "Secret", "metadata": {"name": "kubedoor-ai-security"},
                                             "data": {"AI_ENCRYPTION_KEY": "do-not-replace"}}
    objects["service/kubedoor-web"] = {"kind": "Service", "metadata": {"name": "kubedoor-web"},
                                      "spec": {"ports": [{"port": 80, "nodePort": 30080}]}}
    return objects


def test_generated_deployments_follow_published_manifests():
    result = subprocess.run([sys.executable, str(ROOT / "tools/build-upgrade-patches.py"), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_upgrade_230_adds_cache_and_preserves_live_configuration(tmp_path, shell):
    cluster = ShellCluster(tmp_path, legacy_objects())
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes", "--context", "production", "--namespace", "custom", shell=shell)
    assert result.returncode == 0, result.stderr
    after = cluster.read()["objects"]
    backup = documents((tmp_path / "kubedoor-upgrade-backup.yaml").read_text(encoding="utf-8"))
    assert len(backup) == 5
    saved_ai = next(obj for obj in backup if obj["metadata"]["name"] == "kubedoor-ai")
    assert saved_ai == before["deployment/kubedoor-ai"]
    for component in ("master", "ai", "alarm", "web"):
        key = "deployment/kubedoor-" + component
        previous, current = before[key]["spec"], after[key]["spec"]
        assert current["replicas"] == previous["replicas"]
        assert current["selector"] == previous["selector"]
        assert current["template"]["metadata"] == previous["template"]["metadata"]
        pod = current["template"]["spec"]
        container = pod["containers"][0]
        assert container["image"] == "private.example:5443/team/kubedoor-" + component + ":" + TARGET
        for field in ("env", "envFrom", "resources"):
            assert container[field] == previous["template"]["spec"]["containers"][0][field]
        assert pod["containers"][1] == previous["template"]["spec"]["containers"][1]
        assert pod["imagePullSecrets"] == [{"name": "private-pull"}]
        assert pod["nodeSelector"] == {"custom": "yes"}
        assert "readinessProbe" in container and "livenessProbe" in container
    ai = after["deployment/kubedoor-ai"]["spec"]
    assert ai["strategy"] == {"type": "Recreate"}
    pod = ai["template"]["spec"]
    assert {"name": "code-cache", "emptyDir": {"sizeLimit": "10Gi"}} in pod["volumes"]
    assert {"name": "code-cache", "mountPath": "/data/code-cache"} in pod["containers"][0]["volumeMounts"]
    assert {"name": "custom", "emptyDir": {}} in pod["volumes"]
    for key in ("configmap/kubedoor-config", "secret/kubedoor-ai-security", "service/kubedoor-web"):
        assert after[key] == before[key]
    nginx = after["configmap/nginx-config"]["data"]["nginx.conf"]
    assert "location ^~ /api/admin/agent-installations" in nginx
    assert "location /custom { return 200; }" in nginx
    assert "access_log /var/log/nginx/access.log ai_safe;" in nginx
    assert all(call["args"][:4] == ["--context", "production", "-n", "custom"] for call in cluster.read()["calls"])
    calls = cluster.read()["calls"]
    first_write = next(i for i, call in enumerate(calls) if call in cluster.mutations())
    assert len([call for call in calls[:first_write] if "--dry-run=server" in call["command"]]) == 4
    assert "kubedoor-setup" not in " ".join(" ".join(call["command"]) for call in cluster.mutations())


def test_preview_shows_cache_and_creates_no_files_or_mutations(tmp_path):
    cluster = ShellCluster(tmp_path, legacy_objects())
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--dry-run", "--tag", "custom-tag", "--image-repo", "mirror.example/team")
    assert result.returncode == 0, result.stderr
    assert "emptyDir:" in result.stdout and "sizeLimit: 10Gi" in result.stdout
    assert "mirror.example/team/kubedoor-ai:custom-tag" in result.stdout
    assert cluster.mutations() == []
    assert cluster.read()["objects"] == before
    assert not (tmp_path / "kubedoor-upgrade-backup.yaml").exists()


def test_upgrade_optional_components_absent_and_completed_web_install(tmp_path):
    objects = legacy_objects(optional=False)
    objects["configmap/kubedoor-setup-state"] = state_object("succeeded")
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("upgrade", "--yes")
    assert result.returncode == 0, result.stderr
    assert "跳过未安装组件：kubedoor-ai" in result.stdout
    assert cluster.read()["objects"]["configmap/kubedoor-setup-state"]["data"]["phase"] == "succeeded"


@pytest.mark.parametrize("failure", ["backup", "api-read", "unfinished", "custom-nginx", "validation"])
def test_preflight_failures_never_modify_cluster(tmp_path, failure):
    objects = legacy_objects()
    options = {}
    if failure == "backup":
        (tmp_path / "kubedoor-upgrade-backup.yaml").write_text("keep-original")
    elif failure == "api-read":
        options["get_error"] = "deployment/kubedoor-ai"
    elif failure == "unfinished":
        objects["configmap/kubedoor-setup-state"] = state_object("running")
    elif failure == "custom-nginx":
        objects["configmap/nginx-config"]["data"]["nginx.conf"] = "custom config"
    else:
        options["dry_run_error"] = "deployment/kubedoor-ai"
    cluster = ShellCluster(tmp_path, objects, **options)
    result = cluster.run("upgrade", "--yes")
    assert result.returncode != 0
    assert cluster.mutations() == []
    if failure == "backup":
        assert (tmp_path / "kubedoor-upgrade-backup.yaml").read_text() == "keep-original"


def test_rollout_failure_is_nonzero_and_retry_reloads_web(tmp_path):
    cluster = ShellCluster(tmp_path, legacy_objects(), rollout_error="deployment/kubedoor-ai")
    result = cluster.run("upgrade", "--yes")
    assert result.returncode != 0 and "升级未就绪" in result.stderr
    assert (tmp_path / "kubedoor-upgrade-backup.yaml").exists()
    assert cluster.read()["objects"]["deployment/kubedoor-web"]["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":2.3.0")
    saved = cluster.read()
    saved.pop("rollout_error")
    cluster.write(saved)
    retry = cluster.run("upgrade", "--yes", "--backup", "retry.yaml")
    assert retry.returncode == 0, retry.stderr
    nginx = cluster.read()["objects"]["configmap/nginx-config"]["data"]["nginx.conf"]
    assert nginx.count("location ^~ /api/admin/agent-installations") == 1
    assert any(call["command"][:3] == ["rollout", "restart", "deployment/kubedoor-web"] for call in cluster.read()["calls"])


@pytest.mark.parametrize("args", [("upgrade", "--render"), ("upgrade", "--export", "x"), ("upgrade", "--nodeport", "30080"), ("--dry-run",)])
def test_upgrade_rejects_mixed_install_flags(tmp_path, args):
    cluster = ShellCluster(tmp_path)
    result = cluster.run(*args)
    assert result.returncode != 0
    assert cluster.read()["calls"] == []
