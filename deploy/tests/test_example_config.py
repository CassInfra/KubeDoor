"""deploy/kubedoor.conf.example must stay renderable and must not carry real environment values."""

import re

import pytest
import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config

EXAMPLE = ROOT / "deploy" / "kubedoor.conf.example"
PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")
PRIVATE_IP = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")


def test_example_has_no_real_environment_values():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert not PRIVATE_IP.search(text), PRIVATE_IP.search(text).group()
    # 这些值只能由使用者自己填写,模板里必须为空
    for key in ("PG_HOST", "PG_PASSWORD", "MSG_TOKEN", "K8S_NAME", "OSS_URL", "DEFAULT_AT", "PROM_URL", "REMOTE_WRITE_URL"):
        assert re.search(rf'^{key}=""', text, re.M), key


@pytest.mark.parametrize("role", ["master", "agent"])
def test_example_renders_after_filling_required_values(tmp_path, role):
    fake_bin, _ = setup_config(tmp_path, True)
    config = tmp_path / "kubedoor.conf"
    config.write_text(
        EXAMPLE.read_text(encoding="utf-8")
        + '\nPG_HOST="pg.example.invalid"\nPG_PASSWORD="test-password"\nK8S_NAME="demo-k8s"\n',
        encoding="utf-8",
    )
    output = tmp_path / "rendered"
    args = [bash_path(ROOT / "deploy/install.sh"), "render", role, "-c", bash_path(config), "-o", bash_path(output)]
    rendered = run_bash(fake_bin, 'exec bash "$@"', *args)
    assert rendered.returncode == 0, rendered.stderr
    files = sorted(output.glob("*.yaml"))
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        leftover = PLACEHOLDER.search(text)
        assert leftover is None, f"{path.name}: {leftover.group() if leftover else ''}"
        assert all(doc["apiVersion"] and doc["kind"] for doc in yaml.safe_load_all(text) if doc)
