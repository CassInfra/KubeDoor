"""Render-only checks: never run kubectl against a cluster."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
BASH = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="bash is required for installer checks")


def bash_path(path):
    value = str(path).replace("\\", "/")
    if os.name == "nt" and len(value) > 1 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def setup_config(tmp_path, mcp, extra=()):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "kubectl"
    fake.write_text("#!/usr/bin/env bash\nprintf 'unexpected kubectl call\\n' >&2\nexit 99\n", encoding="utf-8")
    fake.chmod(0o755)
    config = tmp_path / "test.conf"
    lines = [
        "NAMESPACE=ai-render-test", "IMAGE_REPO=example.invalid/kubedoor",
        "TAG_AI=rename-test",
        "PG_HOST=pg.example.invalid", "PG_PORT=5432", "PG_USER=tester",
        "PG_PASSWORD=test-password", "PG_DATABASE=test", "PROM_K8S_TAG_KEY=k8s",
        "WEB_AUTH_USERS='tester:$apr1$local$unused'", "WEB_RW_USERS=tester",
        "ENABLE_GRAFANA=false", "ENABLE_VICTORIA_METRICS=false",
        "ENABLE_VMALERT=false", "ENABLE_ALERTMANAGER=false",
        "PROM_URL=http://metrics.example.invalid", "PROM_TYPE=Victoria-Metrics-Single",
    ]
    # mcp=None 表示配置里不写 ENABLE_MCP,走默认值
    if mcp is not None:
        lines.append(f"ENABLE_MCP={str(mcp).lower()}")
    lines.extend(extra)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fake_bin, config


def run_bash(fake_bin, script, *args):
    result = subprocess.run(
        [str(BASH), "-c", 'export PATH="$1:$PATH"; shift; ' + script, "ai-test", bash_path(fake_bin), *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    return result


# AI 助手必装;ENABLE_MCP 默认开启。旧配置里残留的 ENABLE_AI=false 不能把 AI 关掉
@pytest.mark.parametrize("mcp,extra", [(True, ()), (False, ()), (None, ()), (False, ("ENABLE_AI=false",))])
def test_ai_always_rendered_and_mcp_toggle(tmp_path, mcp, extra):
    expect_mcp = mcp is not False
    fake_bin, config = setup_config(tmp_path, mcp, extra)
    output = tmp_path / "rendered"
    args = [bash_path(ROOT / "deploy/install.sh"), "render", "master", "-c", bash_path(config), "-o", bash_path(output)]
    rendered = run_bash(fake_bin, 'exec bash "$@"', *args)
    assert rendered.returncode == 0, rendered.stderr
    assert "__" not in (output / "05-ai-security.yaml").read_text(encoding="utf-8")
    secrets_path = Path(str(config) + ".ai-secrets")
    first_secrets = secrets_path.read_bytes()
    repeated = run_bash(fake_bin, 'exec bash "$@"', *args)
    assert repeated.returncode == 0, repeated.stderr
    assert secrets_path.read_bytes() == first_secrets
    files = list(output.glob("*.yaml"))
    documents = [doc for path in files for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
    assert all(doc["apiVersion"] and doc["kind"] for doc in documents)
    assert (output / "30-ai.yaml").exists()
    assert not (output / "30-mcp.yaml").exists()
    web = next(doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "nginx-config")
    proxy = web["data"]["default.conf"]
    assert 'location ^~ /api/ai/ {' in proxy
    assert ('location ~ ^/(mcp|sse|messages)' in proxy) == expect_mcp
    assert "proxy_pass http://kubedoor-ai.ai-render-test:8000;" in proxy
    assert "kubedoor-mcp" not in proxy
    assert "location ^~ /api/ai/internal/" in proxy
    safe_log = web["data"]["nginx.conf"].split("log_format ai_safe", 1)[1].split(";", 1)[0]
    assert "$request_body" not in safe_log and "$request_uri" not in safe_log
    assert all(doc["metadata"]["name"] != "kubedoor-mcp" for doc in documents)
    for doc in documents:
        if doc["kind"] == "Deployment":
            for container in doc["spec"]["template"]["spec"]["containers"]:
                assert all(item["name"] != "ENABLE_AI" for item in container.get("env", []))
    ai_resources = [doc for doc in documents if doc["metadata"]["name"] == "kubedoor-ai"]
    assert {doc["kind"] for doc in ai_resources} == {"Deployment", "Service"}
    for doc in ai_resources:
        assert doc["metadata"]["labels"]["app"] == "kubedoor-ai"
        if doc["kind"] == "Service":
            assert doc["spec"]["selector"] == {"app": "kubedoor-ai"}
        elif doc["kind"] == "Deployment":
            assert doc["spec"]["replicas"] == 1
            assert doc["spec"]["strategy"] == {"type": "Recreate"}
            assert doc["spec"]["selector"]["matchLabels"] == {"app": "kubedoor-ai"}
            assert doc["spec"]["template"]["metadata"]["labels"] == {"app": "kubedoor-ai"}
            container = doc["spec"]["template"]["spec"]["containers"][0]
            assert container["name"] == "kubedoor-ai"
            assert container["image"] == "example.invalid/kubedoor/kubedoor-ai:rename-test"
            refs = [item["valueFrom"]["secretKeyRef"]["key"] for item in container["env"] if "valueFrom" in item]
            assert set(refs) == {"AI_INTERNAL_TOKEN", "AI_ENCRYPTION_KEY"}


def test_install_refuses_overwriting_deployed_encryption_key(tmp_path):
    fake_bin, config = setup_config(tmp_path, True)
    local_key = "1" * 64
    secrets_path = Path(str(config) + ".ai-secrets")
    secrets_path.write_text(f"AI_INTERNAL_TOKEN={local_key}\nAI_ENCRYPTION_KEY={local_key}\n", encoding="utf-8")
    # Source only function definitions; replace kc so this test cannot reach Kubernetes.
    script = '''source <(sed '$d' "$1"); CONFIG_FILE="$2"; NAMESPACE=test;
args=(master); kc() { printf '%s %s' "$(printf '%s' "$3" | base64)" "$(printf '%s' "$3" | base64)"; };
ensure_ai_secrets'''
    # Bash function parameters are its own; use a fixed harmless deployed key.
    script = script.replace('"$3"', '"' + "2" * 64 + '"')
    result = run_bash(fake_bin, script, bash_path(ROOT / "deploy/install.sh"), bash_path(config))
    assert result.returncode != 0
    assert "Secret" in result.stderr
    assert secrets_path.read_text(encoding="utf-8") == f"AI_INTERNAL_TOKEN={local_key}\nAI_ENCRYPTION_KEY={local_key}\n"
