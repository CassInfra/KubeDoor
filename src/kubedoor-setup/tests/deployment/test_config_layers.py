"""Published defaults work with compact configs and legacy per-install overrides."""

import re

import pytest
import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config
from test_legacy_general_settings import payload

# 发布默认值以 deploy/install.sh 顶部的镜像配置区为准
PUBLISHED_VMAGENT = re.search(r'^IMAGE_VMAGENT="([^"]+)"', (ROOT / "deploy/install.sh").read_text(encoding="utf-8"),
                              re.M).group(1)


def load_settings(fake_bin, config, defaults_root=None):
    # Load only function definitions; this cannot make a Kubernetes request.
    script = '''source <(sed '$d' "$1")
REPO_ROOT="$2"
CONFIG_FILE="$3"
load_config >/dev/null
printf '%s\\n' "$NAMESPACE" "$KUBE_CONTEXT" "$IMAGE_VMAGENT" "$ENABLE_MCP" "$ALERTMANAGER_EXTURL"
'''
    result = run_bash(
        fake_bin, script,
        bash_path(ROOT / "tools/legacy-install/install.sh"),
        bash_path(defaults_root or ROOT),
        bash_path(config),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_defaults_and_user_overrides_are_loaded_in_order(tmp_path):
    fake_bin, _ = setup_config(tmp_path, True)
    config = tmp_path / "user.conf"
    config.write_text("# An empty user config uses published defaults.\n", encoding="utf-8")
    assert load_settings(fake_bin, config) == [
        "kubedoor", "", PUBLISHED_VMAGENT, "true", "",
    ]
    published = tmp_path / "published"
    for relative in ("deploy/install.sh", "src/kubedoor-setup/resources/kubedoor.defaults.conf"):
        target = published / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
    with (published / "src/kubedoor-setup/resources/kubedoor.defaults.conf").open("a", encoding="utf-8") as stream:
        stream.write('\nALERTMANAGER_EXTURL="https://alerts.example.invalid"\n')
    config.write_text(
        'NAMESPACE="custom-kubedoor"\n'
        'KUBE_CONTEXT="production"\n'
        'IMAGE_VMAGENT="registry.example.invalid/vmagent:pinned"\n'
        'ENABLE_MCP="false"\n'
        'ALERTMANAGER_EXTURL=""\n',
        encoding="utf-8",
    )
    assert load_settings(fake_bin, config, published) == [
        "custom-kubedoor", "production", "registry.example.invalid/vmagent:pinned", "false", "",
    ]


@pytest.mark.parametrize("role", ["master", "agent"])
def test_compact_config_renders_from_another_directory(tmp_path, role):
    fake_bin, _ = setup_config(tmp_path, True)
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    config = settings_dir / "cluster.conf"
    # These values are intentionally absent: namespace, versions, images, PG defaults,
    # and component switches must all come from the published repository resources.
    config.write_text(
        'PG_HOST="pg.example.invalid"\n'
        'PG_PASSWORD="test-password"\n'
        'PROM_URL="http://metrics.example.invalid:8428"\n'
        'K8S_NAME="demo-k8s"\n'
        'MASTER_WS="wss://agent:password@kubedoor.example.invalid"\n'
        'REMOTE_WRITE_URL="http://metrics.example.invalid:8428/api/v1/write"\n',
        encoding="utf-8",
    )
    output = tmp_path / "rendered"
    result = run_bash(
        fake_bin, 'cd "$1"; shift; exec bash "$@"',
        bash_path(tmp_path), bash_path(ROOT / "tools/legacy-install/install.sh"),
        "render", role, "-c", bash_path(config), "-o", bash_path(output),
    )
    assert result.returncode == 0, result.stderr
    objects = [obj for path in output.glob("*.yaml")
               for obj in yaml.safe_load_all(path.read_text(encoding="utf-8")) if obj]
    assert objects
    resources = {(obj["kind"], obj["metadata"]["name"]): obj for obj in objects}
    assert resources["Namespace", "kubedoor"]
    for obj in objects:
        if "namespace" in obj["metadata"]:
            assert obj["metadata"]["namespace"] == "kubedoor"
        if obj["kind"] in ("Deployment", "DaemonSet"):
            assert all(c["image"] for c in obj["spec"]["template"]["spec"]["containers"])
    if role == "master":
        data = resources["ConfigMap", "kubedoor-config"]["data"]
        assert data["PG_PORT"] == "5432"
        assert data["PG_USER"] == data["PG_DATABASE"] == "kubedoor"
        # 只剩数据库连接：其余配置在 master 就绪后写进数据库（见 test_legacy_general_settings）
        assert all(key.startswith("PG_") for key in data)
        assert resources["Deployment", "vmalert"]
    else:
        assert resources["Deployment", "vmagent"]
        assert resources["Deployment", "kube-state-metrics"]
        assert resources["DaemonSet", "node-exporter"]


def test_legacy_overrides_reach_master_and_web_agent_defaults(tmp_path):
    fake_bin, config = setup_config(tmp_path, False, (
        'NAMESPACE="custom-kubedoor"',
        'IMAGE_VMAGENT="registry.example.invalid/vmagent:pinned"',
        'IMAGE_VMALERT="registry.example.invalid/vmalert:pinned"',
        'ENABLE_VMALERT="true"',
        'ALERTMANAGER_EXTURL=""',
    ))
    output = tmp_path / "rendered"
    result = run_bash(
        fake_bin, 'exec bash "$@"',
        bash_path(ROOT / "tools/legacy-install/install.sh"), "render", "master",
        "-c", bash_path(config), "-o", bash_path(output),
    )
    assert result.returncode == 0, result.stderr
    resources = {(obj["kind"], obj["metadata"]["name"]): obj
                 for path in output.glob("*.yaml")
                 for obj in yaml.safe_load_all(path.read_text(encoding="utf-8")) if obj}
    # 网页安装 Agent 的采集镜像随配置写进数据库的通用配置；空的 Alertmanager 外部地址不带
    settings = payload(fake_bin, config)
    assert '"image_vmagent": "registry.example.invalid/vmagent:pinned"' in settings["AGENT_INSTALL_DEFAULTS"]
    assert "ALERTMANAGER_EXTURL" not in settings
    assert resources["ConfigMap", "kubedoor-config"]["metadata"]["namespace"] == "custom-kubedoor"
    assert resources["Deployment", "vmalert"]["spec"]["template"]["spec"]["containers"][0]["image"] == "registry.example.invalid/vmalert:pinned"
    assert "location ~ ^/(mcp|sse|messages)" not in resources["ConfigMap", "nginx-config"]["data"]["nginx.conf"]
