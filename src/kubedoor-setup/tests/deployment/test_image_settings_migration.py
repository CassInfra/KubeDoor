"""Legacy image settings survive upgrades without being rendered into new resources."""

import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

import pytest
import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config


HELPER = ROOT / "tools/legacy-install/image-settings-migration.py"
spec = importlib.util.spec_from_file_location("image_settings_migration", HELPER)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

RULES = {"default": {"isOperationAllowed": False}}
REGISTRY = {"registry.example.invalid": {"default": {"ak": "test-user", "sk": "test-secret"}}}


def fields(previous=None, rules=None, registries=None):
    return [json.dumps(previous or {}), "x" if rules is not None else "", json.dumps(rules) if rules is not None else "",
            "x" if registries is not None else "", json.dumps(registries) if registries is not None else ""]


def test_prepare_local_settings_override_old_cluster_and_survive_retry(tmp_path):
    config = tmp_path / "cluster.conf"
    config.write_text("PG_HOST=pg.example.invalid\n", encoding="utf-8")
    previous = {"data": {"UPDATE_IMAGE": json.dumps({"default": {"isOperationAllowed": True}}),
                         "REGISTRY_SECRET": json.dumps(REGISTRY)}}
    assert migration.prepare(config, fields(previous, rules=RULES))
    pending = migration.snapshot_path(config)
    assert migration.read_snapshot(pending) == {"update_image": RULES, "registry_secret": REGISTRY}
    if os.name != "nt":
        assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    # After an interrupted rollout, the new ConfigMap no longer has legacy keys.
    assert migration.prepare(config, fields({"data": {"PG_HOST": "pg.example.invalid"}}))
    assert migration.read_snapshot(pending)["registry_secret"] == REGISTRY
    # Even if the old ConfigMap remains after failure, a prepared snapshot wins.
    assert migration.prepare(config, fields(previous))
    assert migration.read_snapshot(pending)["update_image"] == RULES


def test_no_snapshot_when_nothing_to_migrate(tmp_path):
    config = tmp_path / "cluster.conf"
    assert not migration.prepare(config, fields())
    assert not migration.snapshot_path(config).exists()


def test_cleanup_preserves_all_unrelated_content_and_permissions(tmp_path):
    config = tmp_path / "cluster.conf"
    prefix = '# Keep this comment\r\nPG_HOST="pg.example.invalid"\r\n'
    middle = "# Preserve the section comment\r\n"
    suffix = 'KUBEDOOR_EXTURL="https://kubedoor.example.invalid"\r\n'
    config.write_bytes((prefix + "UPDATE_IMAGE_JSON='" + json.dumps(RULES, indent=2) + "'\r\n"
                        + middle + "export REGISTRY_SECRET_JSON='" + json.dumps(REGISTRY, indent=2) + "'\r\n"
                        + suffix).encode())
    migration.prepare(config, fields(rules=RULES, registries=REGISTRY))
    migration.cleanup(config)
    assert config.read_bytes() == (prefix + middle + suffix).encode()
    assert not migration.snapshot_path(config).exists()


def test_cleanup_refuses_to_erase_changed_values(tmp_path):
    config = tmp_path / "cluster.conf"
    original = "UPDATE_IMAGE_JSON='{}'\n"
    config.write_text(original, encoding="utf-8")
    migration.prepare(config, fields(rules=RULES))
    with pytest.raises(ValueError, match="有变化"):
        migration.cleanup(config)
    assert config.read_text() == original
    assert migration.snapshot_path(config).exists()


def test_invalid_legacy_json_does_not_replace_valid_snapshot(tmp_path):
    config = tmp_path / "cluster.conf"
    migration.prepare(config, fields(rules=RULES))
    pending = migration.snapshot_path(config)
    before = pending.read_bytes()
    with pytest.raises(ValueError):
        migration.prepare(config, ["{}", "x", "invalid-json-test-secret", "", ""])
    assert pending.read_bytes() == before


def test_installer_migrates_through_stdin_and_cleans_only_after_success(tmp_path):
    fake_bin, config = setup_config(tmp_path, True)
    config.write_text("UPDATE_IMAGE_JSON='" + json.dumps(RULES) + "'\n"
                      + "REGISTRY_SECRET_JSON='" + json.dumps(REGISTRY) + "'\n", encoding="utf-8")
    captured = tmp_path / "stdin.json"
    script = '''source <(sed '$d' "$1")
SCRIPT_DIR="$2"; CONFIG_FILE="$3"; PYTHON_BIN="$4"; NAMESPACE=kubedoor
source "$CONFIG_FILE"
kc() {
  if [[ "$*" == *"get configmap"* ]]; then printf '{}';
  elif [[ "$*" == *"exec -i"* ]]; then cat > "$5"; printf '{"success":true}\\n';
  else return 99; fi
}
prepare_image_settings_migration
complete_image_settings_migration
'''
    # Functions have their own $5; bind the harmless capture path outside kc.
    script = script.replace('source "$CONFIG_FILE"', 'CAPTURE="$5"; source "$CONFIG_FILE"').replace('cat > "$5"', 'cat > "$CAPTURE"')
    result = run_bash(fake_bin, script, bash_path(ROOT / "tools/legacy-install/install.sh"),
                      bash_path(ROOT / "tools/legacy-install"), bash_path(config),
                      str(Path(sys.executable)).replace("\\", "/"), bash_path(captured))
    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text()) == {"update_image": RULES, "registry_secret": REGISTRY}
    assert "test-secret" not in result.stdout + result.stderr
    assert config.read_text() == ""
    assert not migration.snapshot_path(config).exists()


def test_render_does_not_emit_legacy_rules_or_credentials(tmp_path):
    fake_bin, config = setup_config(tmp_path, True, (
        "UPDATE_IMAGE_JSON='" + json.dumps(RULES) + "'",
        "REGISTRY_SECRET_JSON='" + json.dumps(REGISTRY) + "'",
    ))
    output = tmp_path / "rendered"
    result = run_bash(fake_bin, 'exec bash "$@"', bash_path(ROOT / "tools/legacy-install/install.sh"),
                      "render", "master", "-c", bash_path(config), "-o", bash_path(output))
    assert result.returncode == 0, result.stderr
    docs = [doc for path in output.glob("*.yaml") for doc in yaml.safe_load_all(path.read_text()) if doc]
    data = next(doc["data"] for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "kubedoor-config")
    assert "UPDATE_IMAGE" not in data
    assert "REGISTRY_SECRET" not in data
    assert "test-secret" not in "".join(path.read_text() for path in output.glob("*.yaml"))
    assert not migration.snapshot_path(config).exists()


def test_failed_import_retains_pending_snapshot_and_original_config(tmp_path):
    fake_bin, config = setup_config(tmp_path, True)
    original = "REGISTRY_SECRET_JSON='" + json.dumps(REGISTRY) + "'\n"
    config.write_text(original, encoding="utf-8")
    migration.prepare(config, fields(registries=REGISTRY))
    result = run_bash(fake_bin, '''source <(sed '$d' "$1")
SCRIPT_DIR="$2"; CONFIG_FILE="$3"; PYTHON_BIN="$4"; NAMESPACE=kubedoor
kc() { return 1; }
complete_image_settings_migration
''', bash_path(ROOT / "tools/legacy-install/install.sh"), bash_path(ROOT / "tools/legacy-install"),
                      bash_path(config), str(Path(sys.executable)).replace("\\", "/"))
    assert result.returncode != 0
    assert "test-secret" not in result.stdout + result.stderr
    assert config.read_text() == original
    assert migration.snapshot_path(config).exists()
