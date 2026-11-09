"""Embed managed Deployment fields in the standalone installer; run after template edits.

Runtime users only need sh and kubectl. Developer generation requires PyYAML.
Connection settings, replicas, resources, selectors and annotations stay deployment-specific.
"""
from copy import deepcopy
from pathlib import Path
import argparse
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
START = "# BEGIN GENERATED DEPLOYMENT UPGRADE PATCHES"
END = "# END GENERATED DEPLOYMENT UPGRADE PATCHES"
FILES = {"master": "20-master.yaml", "ai": "30-ai.yaml", "alarm": "25-alarm.yaml", "web": "40-web.yaml"}


def deployment_patch(component):
    source = (ROOT / "src/kubedoor-setup/resources/manifests/master" / FILES[component]).read_text(encoding="utf-8")
    # This placeholder is an optional Service line, outside the Deployment.
    source = source.replace("__WEB_NODEPORT_LINE__", "")
    deployment = next(obj for obj in yaml.safe_load_all(source) if obj and obj["kind"] == "Deployment")
    spec = deepcopy(deployment["spec"])
    for key in ("replicas", "selector"):
        spec.pop(key, None)
    spec["template"].pop("metadata", None)
    for container in spec["template"]["spec"]["containers"]:
        container["image"] = "${target}"
        container.pop("resources", None)
        container.pop("env", None)
        container.pop("envFrom", None)
    if spec.get("strategy", {}).get("type") == "Recreate":
        spec["strategy"]["rollingUpdate"] = None
    text = yaml.safe_dump({"spec": spec}, sort_keys=False, allow_unicode=True).replace("__NAMESPACE__", "${NAMESPACE}")
    if re.search(r"__[A-Z_]+__", text):
        raise ValueError("unresolved deployment placeholder")
    shell_text = text.replace("${target}", "").replace("${NAMESPACE}", "")
    if "$" in shell_text or "`" in shell_text:
        raise ValueError("deployment patch needs explicit shell escaping")
    return text


def generated_block():
    parts = [START, "deployment_upgrade_patch() {", '    case "$1" in']
    for component in FILES:
        parts += [f"        kubedoor-{component})", "            yaml <<EOF", deployment_patch(component).rstrip(), "EOF", "            ;;" ]
    parts += ['        *) die "未知升级组件：$1" ;;', "    esac", "}", END]
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    entry = ROOT / "deploy/install.sh"
    original = entry.read_text(encoding="utf-8")
    block = generated_block()
    if START not in original:
        updated = original.replace("upgrade_nginx_patch() {", block + "\n\nupgrade_nginx_patch() {")
    else:
        updated = original[:original.index(START)] + block + original[original.index(END) + len(END):]
    if args.check:
        if updated != original:
            raise SystemExit("Deployment 升级模板已过期，请运行 python tools/build-upgrade-patches.py")
    else:
        entry.write_text(updated, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
