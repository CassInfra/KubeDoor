"""通知改在控制台「系统管理 → 消息通知」配置后的渲染检查（网页安装和旧脚本两条路径）：
不再渲染 MSG_TYPE / MSG_TOKEN / DEFAULT_AT；Alertmanager 的 webhook 固定指向 kubedoor-alarm 的 /msg/kubedoor；
kubedoor-alarm 由模板带上内部 token（向 master 取通知通道），只有一份。"""
import json
import sys

import pytest
import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config

sys.path.insert(0, str(ROOT / "src/kubedoor-setup"))
from kubedoor_setup.render import render_master as render_web

LEGACY_KEYS = ("MSG_TYPE", "MSG_TOKEN", "DEFAULT_AT")
TEMPLATES = ROOT / "src/kubedoor-setup/resources"
SECRETS = {"internal_token": "a" * 64, "encryption_key": "b" * 64, "agent_password": "agent-secret", "grafana_password": "grafana-secret"}
ALARM_TOKEN = {"name": "AI_INTERNAL_TOKEN", "valueFrom": {"secretKeyRef": {"name": "kubedoor-ai-security", "key": "AI_INTERNAL_TOKEN"}}}


def web_config():
    return {
        "postgres": {"host": "db.example.invalid", "port": 5432, "database": "kubedoor", "user": "kubedoor", "password": "pg-secret", "sslmode": "disable"},
        "metrics": {"type": "Victoria-Metrics-Single", "scheme": "http", "host": "metrics.example.invalid", "tenant": "0", "user": "", "password": "",
                    "read_url": "http://metrics.example.invalid:8428", "write_url": "http://metrics.example.invalid:8428/api/v1/write",
                    "tag_key": "cluster"},
        "admin": {"username": "kubedoor", "password": "admin-secret"},
        "public_url": "https://kubedoor.example.invalid",
        "components": {"alerting": True},
        # 告警通知只由安装服务在最后保存到 master，不进任何渲染结果
        "notification": {"type": "wecom", "token": "wecom-render-token-0123456789", "at": "zhangsan"},
    }


def legacy_render(tmp_path, extra=()):
    fake_bin, config = setup_config(tmp_path, True, ("ENABLE_VMALERT=true", "ENABLE_ALERTMANAGER=true", *extra))
    output = tmp_path / "rendered"
    result = run_bash(fake_bin, 'exec bash "$@"', bash_path(ROOT / "tools/legacy-install/install.sh"),
                      "render", "master", "-c", bash_path(config), "-o", bash_path(output))
    assert result.returncode == 0, result.stderr
    texts = [path.read_text(encoding="utf-8") for path in sorted(output.glob("*.yaml"))]
    docs = {(doc["kind"], doc["metadata"]["name"]): doc for text in texts for doc in yaml.safe_load_all(text) if doc}
    return docs, "\n".join(texts), result


def check_notification_resources(docs, namespace, tag_key):
    assert not set(LEGACY_KEYS) & set(docs["ConfigMap", "kubedoor-config"]["data"])
    source = docs["ConfigMap", "alertmanager-config"]["data"]["config.yml"]
    assert "__" not in source
    alertmanager = yaml.safe_load(source)
    receivers = {receiver["name"]: receiver for receiver in alertmanager["receivers"]}
    assert receivers["kubedoor-alarm"]["webhook_configs"] == [
        {"url": f"http://kubedoor-alarm.{namespace}/msg/kubedoor", "send_resolved": True}]
    assert receivers["kubedoor-alarm-store"]["webhook_configs"][0]["url"] == f"http://kubedoor-alarm.{namespace}/alert/store"
    # 按集群标签分组：一批告警属于同一个集群，kubedoor-alarm 才能按集群选通道
    assert tag_key in alertmanager["route"]["routes"][-1]["group_by"]
    container = docs["Deployment", "kubedoor-alarm"]["spec"]["template"]["spec"]["containers"][0]
    assert [item for item in container["env"] if item["name"] == "AI_INTERNAL_TOKEN"] == [ALARM_TOKEN]


def test_templates_no_longer_carry_notification_settings():
    config = (TEMPLATES / "manifests/master/10-config.yaml").read_text(encoding="utf-8")
    defaults = (TEMPLATES / "kubedoor.defaults.conf").read_text(encoding="utf-8")
    for key in LEGACY_KEYS:
        assert key not in config and key not in defaults
    assert "/msg/kubedoor" in (TEMPLATES / "manifests/master/72-alertmanager.yaml").read_text(encoding="utf-8")


def test_web_render_keeps_alarm_webhook_and_has_no_notification_settings():
    objects = render_web(web_config(), SECRETS, "example")
    text = json.dumps(objects, ensure_ascii=False)
    for key in (*LEGACY_KEYS, "wecom-render-token", "zhangsan"):
        assert key not in text
    docs = {(obj["kind"], obj["metadata"]["name"]): obj for obj in objects}
    check_notification_resources(docs, "example", "cluster")


def test_web_render_without_alerting_components_has_no_alarm():
    config = web_config()
    config["components"] = {"alerting": False}
    docs = {(obj["kind"], obj["metadata"]["name"]): obj for obj in render_web(config, SECRETS, "example")}
    assert ("Deployment", "kubedoor-alarm") not in docs and ("ConfigMap", "alertmanager-config") not in docs


def test_legacy_render_keeps_alarm_webhook_and_has_no_notification_settings(tmp_path):
    docs, text, result = legacy_render(tmp_path)
    for key in LEGACY_KEYS:
        assert key not in text
    assert "已不再生效" not in result.stderr
    check_notification_resources(docs, "ai-render-test", "k8s")


@pytest.mark.parametrize("role", ["master", "agent"])
def test_old_notification_settings_only_warn_and_are_not_rendered(tmp_path, role):
    old = ('MSG_TYPE="dingding"', 'MSG_TOKEN="old-legacy-token-0123"', 'DEFAULT_AT="13800000000"')
    agent = ('K8S_NAME="demo-k8s"', 'MASTER_WS="ws://Up4biLko1dNh:agent-secret@kubedoor-master.kubedoor"',
             'REMOTE_WRITE_URL="http://metrics.example.invalid:8428/api/v1/write"')
    fake_bin, config = setup_config(tmp_path, True, (*old, *agent))
    output = tmp_path / "rendered"
    result = run_bash(fake_bin, 'exec bash "$@"', bash_path(ROOT / "tools/legacy-install/install.sh"),
                      "render", role, "-c", bash_path(config), "-o", bash_path(output))
    assert result.returncode == 0, result.stderr
    assert "已不再生效" in result.stderr and "系统管理 → 消息通知" in result.stderr
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in output.glob("*.yaml"))
    for value in ("old-legacy-token-0123", "13800000000", *LEGACY_KEYS):
        assert value not in rendered
