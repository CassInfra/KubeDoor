"""共享安装定义和 vmagent 直连时序库的回归，不连接真实集群。"""
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

# 控制端配置的时序库写入地址（master 的 METRICS_WRITE_URL）
WRITE_URL = "http://monit:vm%40secret@vm.example.com:8428/api/v1/write"


def objects(options=None, password="p@ss:'\"$&/word", write_url=WRITE_URL):
    raw = {"env": "demo", "master_url": "https://kubedoor.example.com:8443", **(options or {})}
    opts = bundle.normalize_options(raw, {**bundle.defaults(), bundle.WRITE_KEY: write_url})
    return {(o["kind"], o["metadata"]["name"]): o for o in bundle.render_agent(opts, username="agent-demo", password=password)}


def vmagent_args(docs):
    return docs["Deployment", "vmagent"]["spec"]["template"]["spec"]["containers"][0]["args"]


def test_credentials_are_secret_and_urls_are_encoded(monkeypatch):
    # 通知改由 master 统一发送：即使 master 环境里还残留旧的 MSG_TOKEN，也不能写进业务集群
    monkeypatch.setenv("MSG_TOKEN", "notify-secret")
    monkeypatch.setenv("MSG_TYPE", "dingding")
    docs = objects()
    cfg = docs["ConfigMap", "kubedoor-agent-config"]["data"]
    assert cfg == {"PROM_K8S_TAG_VALUE": "demo"}
    data = docs["Secret", "kubedoor-agent-credentials"]["data"]
    assert set(data) == {"KUBEDOOR_MASTER", "OSS_URL"}
    assert base64.b64decode(data["KUBEDOOR_MASTER"]).decode() == "wss://agent-demo:p%40ss%3A%27%22%24%26%2Fword@kubedoor.example.com:8443"
    assert "notify-secret" not in json.dumps(list(docs.values()))
    assert "MSG_TYPE" not in json.dumps(list(docs.values())) and "MSG_TOKEN" not in json.dumps(list(docs.values()))
    # vmagent 默认直连控制端配置的时序库写入地址；账号口令拆出来放进 Secret，不在启动参数里
    args = vmagent_args(docs)
    assert "-remoteWrite.url=http://vm.example.com:8428/api/v1/write" in args
    assert "-remoteWrite.basicAuth.username=monit" in args
    assert "-remoteWrite.basicAuth.passwordFile=/etc/kubedoor-metrics/password" in args
    assert "p@ss" not in json.dumps(args) and "vm@secret" not in json.dumps(args) and "vm%40secret" not in json.dumps(args)
    secret = docs["Secret", "kubedoor-metrics-credentials"]["data"]
    assert base64.b64decode(secret["password"]).decode() == "vm@secret"
    assert "agent/metrics/write" not in json.dumps(list(docs.values()))


def test_write_url_override_takes_priority_over_master_default():
    docs = objects({"remote_write_url": "https://writer:w%21x@other.example.com/insert/0/prometheus/api/v1/write"})
    args = vmagent_args(docs)
    assert "-remoteWrite.url=https://other.example.com/insert/0/prometheus/api/v1/write" in args
    assert "-remoteWrite.basicAuth.username=writer" in args
    password = base64.b64decode(docs["Secret", "kubedoor-metrics-credentials"]["data"]["password"]).decode()
    assert password == "w!x"
    assert "vm.example.com" not in json.dumps(list(docs.values()))


def test_write_url_without_account_needs_no_metrics_secret():
    docs = objects(write_url="http://vm.example.com:8428/api/v1/write")
    args = vmagent_args(docs)
    assert "-remoteWrite.url=http://vm.example.com:8428/api/v1/write" in args
    assert not any(arg.startswith("-remoteWrite.basicAuth") for arg in args)
    assert ("Secret", "kubedoor-metrics-credentials") not in docs


def test_full_mode_needs_a_write_url_but_agent_only_does_not():
    with pytest.raises(ValueError, match="时序库写入地址"):
        objects(write_url="")
    docs = objects({"mode": "agent"}, write_url="")
    assert ("Deployment", "vmagent") not in docs
    assert ("Secret", "kubedoor-metrics-credentials") not in docs


def test_master_write_url_comes_from_environment_and_is_never_public(monkeypatch):
    monkeypatch.setenv("METRICS_WRITE_URL", WRITE_URL)
    # 明文 ConfigMap 里的 AGENT_INSTALL_DEFAULTS 不能指定写入地址
    monkeypatch.setenv("AGENT_INSTALL_DEFAULTS", json.dumps({bundle.WRITE_KEY: "http://other.invalid/write", "metrics_available": False}))
    options = bundle.normalize_options({"env": "demo", "master_url": "https://kd.example.com",
                                        bundle.WRITE_KEY: "http://browser.invalid/write"})
    assert options[bundle.WRITE_KEY] == WRITE_URL and options["metrics_available"] is True
    public = bundle.public_options(options)
    assert bundle.WRITE_KEY not in public and public["metrics_available"] is True
    assert "vm%40secret" not in json.dumps(public) and "vm.example.com" not in json.dumps(public)
    monkeypatch.delenv("METRICS_WRITE_URL")
    unavailable = bundle.defaults()
    assert unavailable[bundle.WRITE_KEY] == "" and unavailable["metrics_available"] is False


def test_invalid_master_write_url_is_reported_without_leaking_it(monkeypatch):
    monkeypatch.setenv("METRICS_WRITE_URL", "file:///tmp/secret-token")
    with pytest.raises(ValueError) as error:
        bundle.normalize_options({"env": "demo", "master_url": "https://kd.example.com"})
    assert "METRICS_WRITE_URL" in str(error.value) and "secret-token" not in str(error.value)


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
    assert checksum(one) != checksum(objects(write_url="http://monit:changed@vm.example.com:8428/api/v1/write"))
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


def test_metadata_carries_no_write_address_or_credentials():
    metadata = bundle.master_metadata({"KUBEDOOR_EXTURL": "https://kd.example.com", "METRICS_WRITE_URL": WRITE_URL,
                                       "IMAGE_REPO": "registry.example.com/kd"})
    assert metadata["master_url"] == "https://kd.example.com" and metadata["image_repo"] == "registry.example.com/kd"
    assert "metrics_available" not in metadata and bundle.WRITE_KEY not in metadata
    assert "vm%40secret" not in json.dumps(metadata) and "vm.example.com" not in json.dumps(metadata)


def test_master_without_write_address_renders_empty_value(tmp_path):
    docs, _ = render_master(tmp_path, ("KUBEDOOR_EXTURL=https://kubedoor.example.com",))
    data = docs["ConfigMap", "kubedoor-config"]["data"]
    assert data["METRICS_WRITE_URL"] == ""
    assert "metrics_available" not in json.loads(data["AGENT_INSTALL_DEFAULTS"])
    assert all(name != "kubedoor-metrics-gateway" for _, name in docs)


def test_master_uses_external_metrics_without_deploying_storage(tmp_path):
    docs, _ = render_master(tmp_path, ("METRICS_WRITE_URL=http://metrics.example.invalid:8428/api/v1/write",))
    data = docs["ConfigMap", "kubedoor-config"]["data"]
    assert data["PROM_URL"] == "http://metrics.example.invalid"
    assert data["PROM_TYPE"] == "Victoria-Metrics-Single"
    # 网页安装 Agent 时 vmagent 默认直连这个地址，master 从 kubedoor-config 读取
    assert data["METRICS_WRITE_URL"] == "http://metrics.example.invalid:8428/api/v1/write"
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


def test_web_entry_has_no_metrics_gateway(tmp_path):
    docs, _ = render_master(tmp_path, ("METRICS_WRITE_URL=https://writer:p%40ss@vm.example.com/insert/0/prometheus/api/v1/write",))
    assert ("Secret", "kubedoor-metrics-gateway") not in docs
    conf = docs["ConfigMap", "nginx-config"]["data"]["nginx.conf"]
    assert "/agent/metrics/write" not in conf and "_kd_auth/metrics" not in conf and "metrics.conf" not in conf
    assert "writer:" not in conf and "p%40ss" not in conf
    assert "location ^~ /api/admin/agent-installations" in conf
    web = docs["Deployment", "kubedoor-web"]["spec"]["template"]["spec"]
    assert all(volume["name"] != "metrics-gateway" for volume in web["volumes"])


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
