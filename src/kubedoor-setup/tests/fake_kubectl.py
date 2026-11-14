"""Small stateful Kubernetes CLI double for exercising the real shell installer."""
import base64
import json
import os
from pathlib import Path
import re
import sys

import yaml


OWNER = "setup.kubedoor.io/installation-id"
ID = "718df732-9671-4144-8033-1c531ee41813"
STATE_FILE = Path(os.environ["KUBEDOOR_FAKE_CLUSTER"])
cluster = json.loads(STATE_FILE.read_text(encoding="utf-8"))
# master 容器里 python3 -m general_settings migrate 成功时 stdout 最后一行的汇总（只有配置项名称和状态）
MIGRATE_OK = {"ok": True, "general_settings": {"imported": ["prom_url", "kubedoor_exturl"], "present": [], "invalid": []},
              "notification": "absent", "image_settings": {"rules": "imported", "registries": "absent"},
              "event_rules": "imported"}


def save():
    STATE_FILE.write_text(json.dumps(cluster), encoding="utf-8")


def fail(message):
    print(message, file=sys.stderr)
    save()
    raise SystemExit(1)


def kind_name(value):
    value = value.lower().split(".")[0]
    return {"cm": "configmap", "deploy": "deployment", "sa": "serviceaccount",
            "svc": "service", "ns": "namespace", "po": "pod"}.get(value, value.rstrip("s"))


def key(kind, name):
    return kind_name(kind) + "/" + name


def strategic_merge(current, patch, field=""):
    if isinstance(patch, dict):
        result = dict(current or {})
        for name, value in patch.items():
            if value is None:
                result.pop(name, None)
            else:
                result[name] = strategic_merge(result.get(name), value, name)
        return result
    if isinstance(patch, list):
        merge_key = {"volumeMounts": "mountPath", "ports": "containerPort"}.get(field, "name")
        if all(isinstance(item, dict) and merge_key in item for item in patch):
            result = list(current or [])
            for item in patch:
                match = next((i for i, old in enumerate(result) if old.get(merge_key) == item[merge_key]), None)
                if item.get("$patch") == "delete":
                    # strategic merge 的删除指令：按合并键删除列表项，不存在时是空操作
                    if match is not None:
                        result.pop(match)
                elif match is None:
                    result.append(item)
                else:
                    result[match] = strategic_merge(result[match], item)
            return result
    return patch


def parts(expression):
    result, current, escaped, brackets = [], "", False, 0
    for char in expression.lstrip("."):
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "." and not brackets:
            result.append(current)
            current = ""
        else:
            brackets += (char == "[") - (char == "]")
            current += char
    if current:
        result.append(current)
    return result


def values_at(obj, expression):
    values = [obj]
    for token in parts(expression):
        match = re.fullmatch(r"([^\[]*)(?:\[(.*)\])?", token)
        if not match:
            fail("Unsupported JSONPath token: " + token)
        field, selector = match.groups()
        found = []
        for value in values:
            if field:
                value = value.get(field) if isinstance(value, dict) else None
            if value is None:
                continue
            if selector is None:
                found.append(value)
            elif selector == "*":
                found.extend(value)
            elif selector.isdigit():
                found.extend(value[int(selector):int(selector) + 1])
            else:
                condition = re.fullmatch(r'''\?\(@\.([\w.]+)\s*==\s*["']([^"']*)["']\)''', selector)
                if not condition:
                    fail("Unsupported JSONPath selector: " + selector)
                name, wanted = condition.groups()
                found.extend(item for item in value if values_at(item, "." + name) == [wanted])
        values = found
    return values


def scalar(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def output(obj, format):
    if format == "json":
        print(json.dumps(obj))
    elif format == "yaml":
        print(yaml.safe_dump(obj))
    elif format == "name":
        print(key(obj["kind"], obj["metadata"]["name"]))
    elif format.startswith("jsonpath="):
        expression = format.partition("=")[2]
        for token in re.findall(r"\{([^{}]*)\}", expression):
            if token.startswith(('"', "'")):
                print(json.loads(token), end="")
            else:
                print(" ".join(scalar(value) for value in values_at(obj, token)), end="")
    elif format.startswith("go-template="):
        template = format.partition("=")[2]
        if 'index .data "SETUP_KEY"' not in template or "base64decode" not in template:
            fail("Unexpected go template: " + template)
        print(base64.b64decode(obj.get("data", {}).get("SETUP_KEY", "")).decode(), end="")
    else:
        fail("Unexpected output format: " + format)


raw = sys.argv[1:]
args, format, namespace = [], "", "kubedoor"
index = 0
while index < len(raw):
    arg = raw[index]
    if arg in ("-n", "--namespace", "--context", "--request-timeout", "-o", "--output"):
        value = raw[index + 1]
        if arg in ("-o", "--output"):
            format = value
        elif arg in ("-n", "--namespace"):
            namespace = value
        index += 2
        continue
    if arg.startswith(("--context=", "--request-timeout=", "--namespace=")):
        index += 1
        continue
    if arg.startswith(("-o=", "--output=")):
        format = arg.partition("=")[2]
    else:
        args.append(arg)
    index += 1

call = {"args": raw, "command": args}
cluster.setdefault("calls", []).append(call)
verb = args[0]
objects = cluster.setdefault("objects", {})

if verb == "get":
    resource = args[1]
    if "/" in resource:
        kind, name = resource.split("/", 1)
    else:
        kind = resource
        name = args[2] if len(args) > 2 and not args[2].startswith("-") else ""
    if len(args) > 2 and "/" in resource and "/" in args[2]:
        requested = [name for name in args[1:] if "/" in name]
        if cluster.get("backup_error"):
            fail("simulated backup read failure")
        obj = {"apiVersion": "v1", "kind": "List", "items": [objects[name] for name in requested]}
    elif kind_name(kind) == "node":
        obj = {"items": [{"status": {"addresses": [
            {"type": "InternalIP", "address": "192.0.2.10"},
        ]}}]}
    else:
        obj = objects.get(key(kind, name))
    if cluster.get("get_error") == key(kind, name):
        fail("simulated API failure")
    if obj:
        output(obj, format or "name")
elif verb in ("apply", "create"):
    content = sys.stdin.read()
    docs = [doc for doc in yaml.safe_load_all(content) if doc]
    docs = [item for doc in docs for item in (doc["items"] if doc.get("kind") == "List" else [doc])]
    call["objects"] = docs
    for obj in docs:
        metadata = obj["metadata"]
        object_key = key(obj["kind"], metadata["name"])
        if verb == "create" and object_key in objects:
            fail("AlreadyExists")
        if cluster.get("apply_error") == object_key:
            fail("simulated forbidden: " + object_key)
        metadata.setdefault("uid", ID if metadata["name"] == "kubedoor-setup-state" else "uid-" + metadata["name"])
        objects[object_key] = obj
        if obj["kind"] == "Deployment" and metadata["name"] == "kubedoor-setup":
            objects["secret/kubedoor-setup-auth"] = {
                "kind": "Secret", "metadata": {"name": "kubedoor-setup-auth", "annotations": {OWNER: ID}},
                "data": {"SETUP_KEY": base64.b64encode(b"test-install-key").decode()},
            }
        if obj["kind"] == "Service" and metadata["name"] == "kubedoor-web":
            obj["spec"]["ports"][0].setdefault("nodePort", 30080)
elif verb == "annotate":
    kind, name = args[1:3]
    obj = objects[key(kind, name)]
    for arg in args[3:]:
        if "=" in arg and not arg.startswith("-"):
            field, value = arg.split("=", 1)
            obj["metadata"].setdefault("annotations", {})[field] = value
elif verb == "delete":
    if cluster.get("delete_error"):
        fail("Timed out waiting for old job deletion")
    kind, name = args[1:3]
    objects.pop(key(kind, name), None)
    if cluster.get("finish_during_delete"):
        obj = objects["configmap/kubedoor-setup-state"]
        obj["data"]["phase"] = "succeeded"
        obj["data"]["status.json"] = json.dumps({"phase": "succeeded"})
elif verb == "config" and args[1] == "current-context":
    print("test-context")
elif verb == "version":
    print('{"serverVersion":{"gitVersion":"v1.32.0"}}')
elif verb == "patch":
    kind, name = args[1:3]
    patch = yaml.safe_load(args[args.index("--patch") + 1])
    call["patch"] = patch
    if cluster.get("patch_error") == key(kind, name) or cluster.get("patch_error") is True:
        fail("simulated patch failure")
    if "--dry-run=server" in args:
        # "deployment/x $patch" 只让带删除指令的补丁（升级后的清理）试运行失败
        error = cluster.get("dry_run_error")
        if error == key(kind, name) or (error == key(kind, name) + " $patch" and "$patch" in json.dumps(patch)):
            fail("simulated invalid Deployment")
    else:
        objects[key(kind, name)] = strategic_merge(objects[key(kind, name)], patch)
elif verb == "exec":
    # 容器里的命令：exec_result 注入退出码和输出，默认是迁移成功（日志一行 + 汇总一行）
    call["exec"] = raw[raw.index("--") + 1:] if "--" in raw else []
    if "-i" in args or "--stdin" in args:
        call["stdin"] = sys.stdin.read()
    result = cluster.get("exec_result") or {}
    stdout = result.get("stdout", "general_settings: done\n" + json.dumps(MIGRATE_OK))
    if stdout:
        print(stdout)
    if result.get("stderr"):
        print(result["stderr"], file=sys.stderr)
    save()
    raise SystemExit(result.get("code", 0))
elif verb in ("rollout", "wait"):
    if verb == "rollout" and args[1] == "status":
        # "deployment/x#2" 只让这个 Deployment 的第 2 次 rollout status 失败
        seen = cluster.setdefault("rollout_seen", {})
        seen[args[2]] = seen.get(args[2], 0) + 1
        if cluster.get("rollout_error") in (args[2], f"{args[2]}#{seen[args[2]]}"):
            fail("simulated ImagePullBackOff timeout")
else:
    fail("Unexpected kubectl operation: " + " ".join(raw))
save()
