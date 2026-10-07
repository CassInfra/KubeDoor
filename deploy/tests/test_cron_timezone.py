"""CronJob timeZone: rendered by default, removed when the cluster is older than K8S 1.25."""

import pytest
import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config


def collect_cronjob(output):
    documents = yaml.safe_load_all((output / "20-master.yaml").read_text(encoding="utf-8"))
    return next(doc for doc in documents if doc and doc["kind"] == "CronJob" and doc["metadata"]["name"] == "kubedoor-collect")


@pytest.mark.parametrize("cron_timezone,expected", [(None, "Asia/Shanghai"), ("false", None)])
def test_collect_cronjob_timezone_render(tmp_path, cron_timezone, expected):
    fake_bin, config = setup_config(tmp_path, True)
    output = tmp_path / "rendered"
    prefix = f"CRON_TIMEZONE={cron_timezone} " if cron_timezone else ""
    args = [bash_path(ROOT / "deploy/install.sh"), "render", "master", "-c", bash_path(config), "-o", bash_path(output)]
    rendered = run_bash(fake_bin, prefix + 'exec bash "$@"', *args)
    assert rendered.returncode == 0, rendered.stderr
    spec = collect_cronjob(output)["spec"]
    assert spec.get("timeZone") == expected
    assert spec["schedule"] == "0 1 * * *"


@pytest.mark.parametrize("version,supported", [
    ("v1.21.14", False),
    ("v1.24.17-eks-5e0fdde", False),
    ("v1.25.0", True),
    ("v1.31.2+k3s1", True),
    ("", True),  # 取不到版本时按支持处理
])
def test_cron_timezone_support_by_k8s_version(tmp_path, version, supported):
    fake_bin, _ = setup_config(tmp_path, True)
    # 只加载函数定义(去掉最后一行 main "$@"),不会连集群
    script = 'source <(sed \'$d\' "$1"); k8s_supports_cron_timezone "$2"'
    result = run_bash(fake_bin, script, bash_path(ROOT / "deploy/install.sh"), version)
    assert (result.returncode == 0) == supported, result.stderr
