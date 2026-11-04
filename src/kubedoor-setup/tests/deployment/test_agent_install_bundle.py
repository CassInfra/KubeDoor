"""共享安装定义和指标入口回归，不连接真实集群。"""
import base64
import importlib.util
import json
from pathlib import Path, PurePosixPath

import pytest
import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config
from test_auth_render import render_master

spec = importlib.util.spec_from_file_location("agent_bundle", ROOT / "src/kubedoor-master/agent_install/bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def objects(options=None, password="p@ss:'\"$&/word"):
    raw = {"env": "demo", "master_url": "https://kubedoor.example.com:8443", **(options or {})}
    opts = bundle.normalize_options(raw)
    return {(o["kind"], o["metadata"]["name"]): o for o in bundle.render_agent(opts, username="agent-demo", password=password)}


def test_credentials_are_secret_and_urls_are_encoded(monkeypatch):
    monkeypatch.setenv("MSG_TOKEN", "notify-secret")
    docs = objects()
    cfg = docs["ConfigMap", "kubedoor-agent-config"]["data"]
    assert "KUBEDOOR_MASTER" not in cfg and "MSG_TOKEN" not in cfg
    data = docs["Secret", "kubedoor-agent-credentials"]["data"]
    assert base64.b64decode(data["KUBEDOOR_MASTER"]).decode() == "wss://agent-demo:p%40ss%3A%27%22%24%26%2Fword@kubedoor.example.com:8443"
    assert base64.b64decode(data["MSG_TOKEN"]).decode() == "notify-secret"
    args = docs["Deployment", "vmagent"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "-remoteWrite.url=https://kubedoor.example.com:8443/agent/metrics/write" in args
    assert "-remoteWrite.basicAuth.passwordFile=/etc/kubedoor-metrics/password" in args
    assert "p@ss" not in json.dumps(args)


def test_mode_and_reuse_keep_scrape_configuration_consistent():
    assert ("Deployment", "vmagent") not in objects({"mode": "agent"})
    reused = objects({"ksm": "reuse", "node_exporter": "reuse"})
    assert ("Deployment", "kube-state-metrics") not in reused
    assert ("DaemonSet", "node-exporter") not in reused
    scrape = yaml.safe_load(reused["ConfigMap", "vmagent-config"]["data"]["scrape.yml"])
    ksm = next(j for j in scrape["scrape_configs"] if j["job_name"] == "kube-state-metrics")
    assert "namespaces" not in ksm["kubernetes_sd_configs"][0]
    assert scrape["global"]["external_labels"] == {"origin_prometheus": "demo"}


def test_config_change_rolls_agent_and_remote_credentials_are_not_public():
    one = objects()
    two = objects({"oss_url": "https://objects.example.com"})
    def checksum(docs):
        return docs["Deployment", "kubedoor-agent"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert checksum(one) != checksum(two)
    raw = bundle.normalize_options({"env": "demo", "master_url": "https://kd.example.com", "remote_write_url": "https://remote:secret@vm.example.com/write?token=hidden"})
    public = bundle.public_options(raw)
    assert public["remote_write_url"] == "https://vm.example.com/write"
    assert "msg_token" not in public


def test_write_url_query_credentials_are_kept_in_secret():
    docs = objects({"remote_write_url": "https://vm.example.com/write?token=private-query-token"})
    deployment = docs["Deployment", "vmagent"]
    assert "private-query-token" not in json.dumps(deployment)
    secret = docs["Secret", "kubedoor-metrics-credentials"]
    assert "private-query-token" in base64.b64decode(secret["data"]["url"]).decode()
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert any(item["name"] == "KUBEDOOR_REMOTE_WRITE_URL" for item in env)


@pytest.mark.parametrize("url", ["file:///tmp/metrics", "https://vm.invalid/write;evil", "https://vm.invalid/$TOKEN"])
def test_metrics_gateway_rejects_config_injection(url):
    with pytest.raises(ValueError):
        bundle.master_metadata({"METRICS_WRITE_URL": url})


def test_gateway_does_not_guess_write_address_from_query_backend(tmp_path):
    docs, _ = render_master(tmp_path, ("KUBEDOOR_EXTURL=https://kubedoor.example.com",))
    defaults = json.loads(docs["ConfigMap", "kubedoor-config"]["data"]["AGENT_INSTALL_DEFAULTS"])
    assert defaults["metrics_available"] is False
    secret = docs["Secret", "kubedoor-metrics-gateway"]["data"]["metrics.conf"]
    assert base64.b64decode(secret).decode().strip() == "return 503;"


def test_empty_metadata_has_no_metrics_backend():
    defaults, proxy = bundle.master_metadata({})
    assert defaults["metrics_available"] is False
    assert proxy == "return 503;"


def test_master_uses_external_metrics_without_deploying_storage(tmp_path):
    docs, _ = render_master(tmp_path, ("METRICS_WRITE_URL=http://metrics.example.invalid:8428/api/v1/write",))
    data = docs["ConfigMap", "kubedoor-config"]["data"]
    assert data["PROM_URL"] == "http://metrics.example.invalid"
    assert data["PROM_TYPE"] == "Victoria-Metrics-Single"
    assert json.loads(data["AGENT_INSTALL_DEFAULTS"])["metrics_available"] is True
    assert all(name != "victoria-metrics" for _, name in docs)
    assert all(kind != "PersistentVolumeClaim" for kind, _ in docs)
    assert not (tmp_path / "rendered" / "70-victoria-metrics.yaml").exists()
    assert "victoria-metrics.ai-render-test" not in json.dumps(list(docs.values()))


@pytest.mark.parametrize("role,missing", [("master", "PROM_URL"), ("agent", "REMOTE_WRITE_URL")])
def test_script_requires_explicit_metrics_addresses(tmp_path, role, missing):
    fake_bin, config = setup_config(tmp_path, True, ("K8S_NAME=demo", "MASTER_WS=wss://agent-demo:password@kubedoor.example.com", f'{missing}=""'))
    result = run_bash(fake_bin, 'exec bash "$@"', bash_path(ROOT / "tools/legacy-install/install.sh"), "render", role, "-c", bash_path(config), "-o", bash_path(tmp_path / "rendered"))
    assert result.returncode != 0
    assert missing in result.stderr


def test_gateway_uses_server_secret_and_verifies_tls(tmp_path):
    docs, _ = render_master(tmp_path, ("METRICS_WRITE_URL=https://writer:p%40ss@vm.example.com/insert/0/prometheus/api/v1/write",))
    proxy = base64.b64decode(docs["Secret", "kubedoor-metrics-gateway"]["data"]["metrics.conf"]).decode()
    assert "proxy_pass https://vm.example.com/insert/0/prometheus/api/v1/write;" in proxy
    assert base64.b64encode(b"writer:p@ss").decode() in proxy
    assert "proxy_ssl_verify on;" in proxy and "proxy_ssl_server_name on;" in proxy
    conf = docs["ConfigMap", "nginx-config"]["data"]["nginx.conf"]
    assert "writer:" not in conf and "p%40ss" not in conf
    assert "location = /agent/metrics/write" in conf and "auth_request /_kd_auth/metrics;" in conf
    assert "location ^~ /api/admin/agent-installations" in conf


def test_script_and_web_share_agent_resources(tmp_path):
    fake_bin, config = setup_config(tmp_path, True, ("K8S_NAME=demo", "MASTER_WS=wss://agent-demo:password@kubedoor.example.com", "TAG_AGENT=2.3.0", "REMOTE_WRITE_URL=https://writer:password@vm.example.com/write"))
    output = tmp_path / "rendered"
    result = run_bash(fake_bin, 'exec bash "$@"', bash_path(ROOT / "tools/legacy-install/install.sh"), "render", "agent", "-c", bash_path(config), "-o", bash_path(output))
    assert result.returncode == 0, result.stderr
    rendered = [o for p in output.glob("*.yaml") for o in yaml.safe_load_all(p.read_text(encoding="utf-8")) if o]
    names = {(o["kind"], o["metadata"]["name"]) for o in rendered}
    assert ("ClusterRole", "kubedoor-state-metrics") in names
    assert ("Secret", "kubedoor-agent-credentials") in names
    assert ("Secret", "kubedoor-metrics-credentials") in names
    config = next(o for o in rendered if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "kubedoor-agent-config")
    assert "KUBEDOOR_MASTER" not in config["data"]


def test_legacy_component_switches_remain_independent():
    options, user, password = bundle._from_vars({"K8S_NAME": "demo", "MASTER_WS": "ws://u:p@kd.example.com", "ENABLE_VMAGENT": "false", "ENABLE_KUBE_STATE_METRICS": "true", "ENABLE_NODE_EXPORTER": "true"})
    docs = bundle.render_agent(options, username=user, password=password)
    names = {o["metadata"]["name"] for o in docs}
    assert "vmagent" not in names
    assert "kube-state-metrics" in names and "node-exporter" in names


def test_packaged_templates_at_shallow_container_path(monkeypatch):
    class ImagePath(PurePosixPath):
        def resolve(self):
            return self

        def is_dir(self):
            return str(self) == '/agent_install/manifests'

    monkeypatch.delenv('AGENT_INSTALL_TEMPLATE_DIR', raising=False)
    monkeypatch.setattr(bundle, 'Path', ImagePath)
    monkeypatch.setattr(bundle, '__file__', '/agent_install/bundle.py')
    assert str(bundle._template_dir()) == '/agent_install/manifests'
