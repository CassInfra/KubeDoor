"""Run the published shell entry itself; Python is only the test harness."""
from functools import lru_cache
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/install.sh"
OWNER = "setup.kubedoor.io/installation-id"
INSTALL_ID = "718df732-9671-4144-8033-1c531ee41813"


def shell_path(kind="sh"):
    override = os.environ.get("KUBEDOOR_TEST_" + kind.upper())
    if override:
        return override
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin") / (kind + ".exe")
        if not candidate.exists():
            candidate = Path("C:/Program Files/Git/usr/bin") / (kind + ".exe")
        return str(candidate) if candidate.exists() else None
    return shutil.which(kind)


def posix_path(path):
    value = str(path).replace("\\", "/")
    if os.name == "nt" and len(value) > 1 and value[1] == ":":
        return "/" + value[0].lower() + value[2:]
    return value


def run_entry(*args, shell="sh", script=SCRIPT, cwd=None, env=None, timeout=45):
    executable = shell_path(shell)
    if not executable:
        pytest.skip(shell + " is required for the published installer checks")
    # MSYS strips quote characters from Windows argv passed to another shell.
    # Source the entry in the selected fresh shell with faithfully quoted args;
    # this preserves the POSIX semantics being exercised, including exit/status.
    command = "set -- " + shlex.join(list(map(str, args))) + "; . " + shlex.quote(posix_path(script))
    return subprocess.run(
        [executable, "-c", command],
        cwd=cwd or ROOT, env={**os.environ, **(env or {})},
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def documents(text):
    result = []
    for obj in yaml.safe_load_all(text):
        if not obj:
            continue
        result.extend(obj["items"] if obj.get("kind") == "List" else [obj])
    return result


@lru_cache(maxsize=16)
def render_bootstrap(*args):
    result = run_entry("--render", *args)
    assert result.returncode == 0, result.stderr
    return documents(result.stdout)


class ShellCluster:
    """A copied standalone script sees only kubectl in its executable search path."""
    def __init__(self, directory, objects=None, **options):
        self.directory = directory
        self.script = directory / "install.sh"
        shutil.copyfile(SCRIPT, self.script)
        self.bin = directory / "bin"
        self.bin.mkdir()
        self.state = directory / "cluster.json"
        saved_objects = copy.deepcopy(objects or {})
        for obj in saved_objects.values():
            obj["metadata"].setdefault("uid", "uid-" + obj["metadata"]["name"])
        self.write({"objects": saved_objects, "calls": [], **options})
        executable = Path(sys.executable).as_posix()
        fake = (Path(__file__).parent / "fake_kubectl.py").as_posix()
        wrapper = self.bin / "kubectl"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(executable) + " " + shlex.quote(fake) + ' "$@"\n', encoding="utf-8", newline="\n")
        wrapper.chmod(0o755)

    def read(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def write(self, state):
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def run(self, *args, shell="sh"):
        return run_entry(*args, shell=shell, script=self.script, cwd=self.directory, env={
            "PATH": posix_path(self.bin), "KUBEDOOR_FAKE_CLUSTER": str(self.state),
            "PYTHON_BIN": "this-command-must-not-exist",
        })

    def mutations(self):
        return [call for call in self.read()["calls"]
                if "--dry-run=server" not in call["command"] and
                (call["command"][0] in ("apply", "create", "delete", "annotate", "patch", "replace")
                 or call["command"][:2] == ["rollout", "restart"])]


def state_object(phase, *, owner=INSTALL_ID):
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "kubedoor-setup-state", "uid": INSTALL_ID, "annotations": {OWNER: owner}},
        "data": {
            "phase": phase, "installation-id": INSTALL_ID, "public-url": "https://kubedoor.example.invalid",
            "state.json": json.dumps({"id": INSTALL_ID, "phase": phase}),
            "status.json": json.dumps({"phase": phase, "installation_id": INSTALL_ID, "public_url": "https://kubedoor.example.invalid"}),
        },
    }


def recovery_objects(condition="Failed"):
    return {
        "configmap/kubedoor-setup-state": state_object("finalizing"),
        "deployment/kubedoor-setup": {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "kubedoor-setup", "annotations": {OWNER: INSTALL_ID}},
            "spec": {"replicas": 0, "template": {"spec": {
                "containers": [{"name": "kubedoor-setup", "image": "private.example/setup:original"}],
                "imagePullSecrets": [{"name": "pull-key"}],
            }}},
        },
        "job/kubedoor-setup-finalize": {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": "kubedoor-setup-finalize", "uid": "old-uid", "annotations": {OWNER: INSTALL_ID}},
            "spec": {"selector": {"matchLabels": {"controller-uid": "old-uid"}}, "template": {"metadata": {"labels": {"controller-uid": "old-uid"}}}},
            "status": {"conditions": [{"type": condition, "status": "True"}]},
        },
    }
